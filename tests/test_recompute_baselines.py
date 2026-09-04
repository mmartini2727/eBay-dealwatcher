"""Tests for scripts/recompute_baselines.py's run_recompute().

Real SQLite under tmp_path. No network.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import SpecResult
from dealwatch.storage.sqlite import connect, record_sighting, store_spec
from recompute_baselines import run_recompute

PROFILE = load_profile(Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml")
BUCKET = "1|intel-10th|16|256"


def sight(conn, item_id, seen_at, price_cents):
    raw = {"itemId": item_id, "title": "t", "price": {"value": f"{price_cents / 100:.2f}"}}
    record_sighting(
        conn,
        item_id,
        dict(profile_id=PROFILE.id, title="t"),
        dict(price_cents=price_cents, raw_json=json.dumps(raw)),
        seen_at,
    )


def seed_fast_bucket(conn, count, *, gone_offset_hours=1, start_price=10000):
    t0 = 1_000_000
    for i in range(count):
        item_id = f"item-{i}"
        sight(conn, item_id, t0, start_price + i * 100)
        store_spec(conn, item_id, SpecResult(spec={}, spec_status="ok", reject_rule_id=None, bucket_key=BUCKET))
        conn.execute(
            "UPDATE listings SET gone_at = ? WHERE item_id = ?",
            (t0 + gone_offset_hours * 3600, item_id),
        )


def test_recompute_writes_a_qualifying_bucket(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_fast_bucket(conn, 12)

    output = run_recompute(PROFILE, conn)

    row = conn.execute(
        "SELECT n, bucket_key FROM baselines WHERE profile_id = ?", (PROFILE.id,)
    ).fetchone()
    assert row["bucket_key"] == BUCKET
    assert row["n"] == 12
    assert "1 bucket(s) qualified" in output


def test_recompute_writes_nothing_below_min_samples(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_fast_bucket(conn, 11)

    output = run_recompute(PROFILE, conn)

    rows = conn.execute("SELECT * FROM baselines WHERE profile_id = ?", (PROFILE.id,)).fetchall()
    assert rows == []
    assert "0 bucket(s) qualified" in output


def test_recompute_run_twice_produces_identical_state(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_fast_bucket(conn, 12)

    run_recompute(PROFILE, conn)
    first = [
        dict(r) for r in conn.execute(
            "SELECT profile_id, bucket_key, n, n_price_only, p10_cents, p25_cents, "
            "p50_cents, fast_hours FROM baselines ORDER BY bucket_key"
        ).fetchall()
    ]

    run_recompute(PROFILE, conn)
    second = [
        dict(r) for r in conn.execute(
            "SELECT profile_id, bucket_key, n, n_price_only, p10_cents, p25_cents, "
            "p50_cents, fast_hours FROM baselines ORDER BY bucket_key"
        ).fetchall()
    ]

    # computed_at deliberately excluded from the comparison - it's a fresh
    # timestamp each run by design; everything else must be byte-identical.
    assert first == second
    assert len(first) == 1


def test_recompute_replaces_a_bucket_that_no_longer_qualifies(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_fast_bucket(conn, 12)
    run_recompute(PROFILE, conn)
    assert len(conn.execute("SELECT * FROM baselines").fetchall()) == 1

    # Drop below min_samples by marking one listing's spec_status stale.
    conn.execute("UPDATE listings SET spec_status = 'stale' WHERE item_id = 'item-0'")
    run_recompute(PROFILE, conn)

    assert conn.execute("SELECT * FROM baselines").fetchall() == []
