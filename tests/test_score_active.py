"""Tests for scripts/score_active.py's run_score_active().

Uses the real profiles/thinkpad-t14.yaml for its seed_baselines fallback
(match: {} -> p25=250, p50=350) and scoring.sanity_floor_pct, the same
approach test_recompute_baselines.py and test_baseline_report.py already
take - store_spec() writes bucket_key/spec_json directly rather than
running the real regex pipeline, since normalize() itself isn't what this
script is testing. Real SQLite under tmp_path. No network.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import SpecResult
from dealwatch.storage.sqlite import connect, record_sighting, store_spec
from score_active import run_score_active

PROFILE = load_profile(Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml")
PROFILE_ID = PROFILE.id
BUCKET = "1|intel-10th|16|256"


def sight(conn, item_id, price_cents, *, seen_at=1_000_000):
    raw = {"itemId": item_id, "title": "t", "price": {"value": f"{price_cents / 100:.2f}"}}
    record_sighting(
        conn,
        item_id,
        dict(profile_id=PROFILE_ID, title="t"),
        dict(price_cents=price_cents, raw_json=json.dumps(raw)),
        seen_at,
    )


def make_active(conn, item_id, price_cents, *, bucket_key=None, spec=None, spec_status="ok"):
    sight(conn, item_id, price_cents)
    store_spec(
        conn, item_id,
        SpecResult(spec=spec or {}, spec_status=spec_status, reject_rule_id=None, bucket_key=bucket_key),
    )


def make_dead(conn, item_id, price_cents, *, bucket_key=None):
    sight(conn, item_id, price_cents)
    store_spec(
        conn, item_id,
        SpecResult(spec={}, spec_status="ok", reject_rule_id=None, bucket_key=bucket_key),
    )
    gone_at = 1_000_000 + 3600
    conn.execute(
        "UPDATE listings SET gone_at = ?, last_seen = ? WHERE item_id = ?",
        (gone_at, gone_at, item_id),
    )


def test_scores_active_listings_sorted_by_ratio_to_p25_ascending(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # Fallback seed is p25=250/p50=350 (profiles/thinkpad-t14.yaml). Neither
    # listing has a bucket_key, so both fall straight to that seed.
    make_active(conn, "expensive", 30000)  # ratio_to_p25 = 300/250 = 1.2
    make_active(conn, "cheap", 20000)      # ratio_to_p25 = 200/250 = 0.8

    output = run_score_active(PROFILE, conn, limit=10)

    cheap_pos = output.index("cheap")
    expensive_pos = output.index("expensive")
    assert cheap_pos < expensive_pos  # cheaper-relative-to-baseline listed first
    assert "2 listing(s) scored, 0 skipped" in output


def test_persists_sanity_flagged_on_the_listings_row(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # Fallback p50=350 (=$350.00); sanity_floor_pct=35 -> flagged under $122.50.
    make_active(conn, "junk", 5000)  # $50, well under the floor

    run_score_active(PROFILE, conn, limit=10)

    row = conn.execute(
        "SELECT sanity_flagged FROM listings WHERE item_id = 'junk'"
    ).fetchone()
    assert row["sanity_flagged"] == 1


def test_listing_with_no_usable_price_is_skipped_not_raised(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # An auction-only row: no price_cents/total_cents at all.
    record_sighting(
        conn, "auction-only",
        dict(profile_id=PROFILE_ID, title="t"),
        dict(price_cents=None, raw_json="{}"),
        1_000_000,
    )
    store_spec(
        conn, "auction-only",
        SpecResult(spec={}, spec_status="ok", reject_rule_id=None, bucket_key=None),
    )

    output = run_score_active(PROFILE, conn, limit=10)  # must not raise

    assert "0 listing(s) scored, 1 skipped (no usable price)" in output


def test_dead_listings_and_non_ok_status_are_excluded(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    make_dead(conn, "gone", 20000)
    make_active(conn, "partial", 20000, spec_status="partial")
    make_active(conn, "ok", 20000)

    output = run_score_active(PROFILE, conn, limit=10)

    assert "1 listing(s) scored" in output
    assert "gone" not in output
    assert "partial" not in output


def test_limit_caps_printed_rows_but_not_the_scored_count(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    for i in range(5):
        make_active(conn, f"item-{i}", 20000 + i * 100)

    output = run_score_active(PROFILE, conn, limit=2)

    assert "5 listing(s) scored" in output
    assert output.count("item-") == 2
