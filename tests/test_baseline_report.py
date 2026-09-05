"""Tests for scripts/baseline_report.py's run_report().

Real SQLite under tmp_path - not opened mode=ro here (run_report takes
whatever connection it's handed; the CLI's use of mode=ro is exercised
manually, same as scripts/normalize_report.py's test file notes). No
network.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import SpecResult
from dealwatch.storage.sqlite import connect, record_sighting, store_spec
from baseline_report import run_report

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


def die(conn, item_id, bucket_key, gone_at, spec_status="ok"):
    # gone_at = last_seen is a production invariant (record_sweep sets both
    # together, storage/sqlite.py) - moved together here too, or every
    # fixture in this file would look "never confirmed by a sweep" to
    # V0.8b's new exclusion regardless of what each test intends. Pass the
    # same value sight() used as gone_at to simulate a never-swept listing
    # instead - see test_never_swept_row_reported_and_excluded below.
    store_spec(conn, item_id, SpecResult(spec={}, spec_status=spec_status, reject_rule_id=None, bucket_key=bucket_key))
    conn.execute(
        "UPDATE listings SET gone_at = ?, last_seen = ? WHERE item_id = ?",
        (gone_at, gone_at, item_id),
    )


def test_pool_section_reports_each_exclusion_stage(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    t0 = 1_000_000

    sight(conn, "ok1", t0, 50000)
    die(conn, "ok1", BUCKET, t0 + 3600)

    sight(conn, "qmark", t0, 50000)
    die(conn, "qmark", "1|?|16|256", t0 + 3600)

    sight(conn, "nobucket", t0, 50000)
    die(conn, "nobucket", None, t0 + 3600)

    output = run_report(PROFILE, conn)

    assert "3 dead listings with spec_status='ok'" in output
    assert "3 were confirmed present by at least one sweep" in output
    assert "2 have a bucket_key at all" in output
    assert "1 of those have no '?' component" in output
    assert "-> 1 final candidates" in output


def test_never_swept_row_reported_and_excluded(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    t0 = 1_000_000

    sight(conn, "neverswept", t0, 50000)
    die(conn, "neverswept", BUCKET, t0)  # same t0 - never confirmed by a sweep

    sight(conn, "ok1", t0, 50000)
    die(conn, "ok1", BUCKET, t0 + 3600)

    output = run_report(PROFILE, conn)

    assert "2 dead listings with spec_status='ok'" in output
    assert "1 were confirmed present by at least one sweep" in output
    assert "-> 1 final candidates" in output


def test_qualifying_bucket_shows_qualifies_marker(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    t0 = 1_000_000
    for i in range(12):
        item_id = f"item-{i}"
        sight(conn, item_id, t0, 10000 + i * 10)
        die(conn, item_id, BUCKET, t0 + 3600)

    output = run_report(PROFILE, conn)

    assert f"{BUCKET}: dead= 12 fast=12/12" in output
    assert "QUALIFIES" in output


def test_near_miss_bucket_shown_without_qualifies_marker(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    t0 = 1_000_000
    for i in range(11):
        item_id = f"item-{i}"
        sight(conn, item_id, t0, 10000 + i * 10)
        die(conn, item_id, BUCKET, t0 + 3600)

    output = run_report(PROFILE, conn)

    assert "fast=11/12" in output
    # This exact bucket's line must not be marked QUALIFIES.
    bucket_line = next(line for line in output.splitlines() if line.strip().startswith(BUCKET))
    assert "QUALIFIES" not in bucket_line


def test_falsification_check_reports_fast_cheaper(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    t0 = 1_000_000
    for i in range(5):
        item_id = f"fast-{i}"
        sight(conn, item_id, t0, 20000 + i * 10)
        die(conn, item_id, BUCKET, t0 + 3600)  # 1h - fast
    for i in range(5):
        item_id = f"slow-{i}"
        sight(conn, item_id, t0, 40000 + i * 10)
        die(conn, item_id, BUCKET, t0 + 100 * 3600)  # 100h - slow

    output = run_report(PROFILE, conn)

    assert "fast cheaper in 1 of 1 buckets" in output
    assert "SLOW CHEAPER" not in output


def test_falsification_check_reports_slow_cheaper_plainly_when_premise_violated(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    t0 = 1_000_000
    # Deliberately backwards: fast listings priced HIGHER than slow ones.
    for i in range(5):
        item_id = f"fast-{i}"
        sight(conn, item_id, t0, 90000 + i * 10)
        die(conn, item_id, BUCKET, t0 + 3600)
    for i in range(5):
        item_id = f"slow-{i}"
        sight(conn, item_id, t0, 10000 + i * 10)
        die(conn, item_id, BUCKET, t0 + 100 * 3600)

    output = run_report(PROFILE, conn)

    assert "fast cheaper in 0 of 1 buckets" in output
    assert "SLOW CHEAPER - premise violated here" in output


def test_falsification_check_with_no_eligible_buckets_does_not_divide_by_zero(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # No dead 'ok' listings at all.
    output = run_report(PROFILE, conn)  # must not raise

    assert "not enough data to check yet" in output
    assert "fast cheaper in 0 of 0 buckets" in output
