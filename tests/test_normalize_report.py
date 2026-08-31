"""Tests for scripts/normalize_report.py's run_report().

Only the computation is tested here (against a real tmp-file SQLite db) -
not the CLI arg parsing or read-only file: URI, which the manual run
already verified. No network.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dealwatch.engine.collector import load_profile
from dealwatch.storage.sqlite import connect, record_sighting
from normalize_report import run_report

PROFILE = load_profile(Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml")


def seed(conn, item_id: str, title: str, seen_at: int, condition_id: int = 3000) -> None:
    raw = {
        "itemId": item_id,
        "title": title,
        "price": {"value": "349.99", "currency": "USD"},
        "conditionId": str(condition_id),
        "buyingOptions": ["FIXED_PRICE"],
    }
    record_sighting(
        conn,
        item_id,
        dict(profile_id=PROFILE.id, title=title, condition_id=condition_id),
        dict(price_cents=34999, buying_options=["FIXED_PRICE"], raw_json=json.dumps(raw)),
        seen_at,
    )


def test_report_counts_spec_statuses_and_reject_hits(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")

    seed(conn, "v1|1|0", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD", 1000)
    seed(conn, "v1|2|0", "LENOVO THINKPAD T14s GEN 2 Ryzen 7 PRO 5850U 16GB 256GB", 1001)
    seed(conn, "v1|3|0", "Lenovo ThinkPad T480 Core i5 16GB 256GB", 1002)

    output = run_report(PROFILE, conn, seed=1)

    assert "rejected" in output
    assert "not_target" in output
    assert "t14s-not-t14: 1 hits" in output
    assert "for-parts-condition: 0 hits" in output  # a rule that never fired still shows


def test_report_excludes_mapping_failures_from_status_counts(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")

    # A raw_json with no price - map_item_summary() will raise for this one.
    bad_raw = {"itemId": "v1|bad|0", "title": "Broken listing"}
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES (?, ?, ?, 'pending', 1000, 1000, 0)",
        ("v1|bad|0", PROFILE.id, "Broken listing"),
    )
    conn.execute(
        "INSERT INTO observations (item_id, observed_at, buying_options, raw_json) "
        "VALUES (?, ?, ?, ?)",
        ("v1|bad|0", 1000, "[]", json.dumps(bad_raw)),
    )

    output = run_report(PROFILE, conn, seed=1)

    assert "1 failed map_item_summary()" in output
