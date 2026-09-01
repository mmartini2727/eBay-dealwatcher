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


def test_bucket_histogram_counts_ok_and_partial_separately(tmp_path):
    # V0.7a: a bucket built entirely from partial listings (missing
    # generation and/or cpu_family - the two bucket_require fields) must
    # never count toward "reaches min_samples", even if it has plenty of
    # raw hits. Old code merged ok+partial into one Counter and overstated
    # baseline coverage this way.
    conn = connect(tmp_path / "dealwatch.db")

    seed(conn, "v1|ok|0", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD", 1000)
    seed(conn, "v1|ok|1", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD v2", 1001)
    for i in range(12):
        # No generation marker, no CPU model - generation and cpu_family
        # both stay null, so this is partial with bucket_key "?|?|16|256".
        seed(conn, f"v1|partial|{i}", "Lenovo ThinkPad T14 16GB RAM 256GB SSD", 1002 + i)

    output = run_report(PROFILE, conn, seed=1)

    assert "1|intel-10th|16|256: 2 ok, 0 partial" in output
    assert "?|?|16|256: 0 ok, 12 partial" in output
    # 12 partial hits alone must not look like reached coverage.
    assert "0 OK-only buckets reach scoring.min_samples=12" in output


def test_generation_disagreement_counts_title_text_against_cpu_model(tmp_path):
    # i5-1335U is a 13th-gen chip (derive: intel-13th -> generation "4").
    # A seller who writes "Gen 1" on that machine is wrong about the
    # generation, not about the CPU model - sellers copy model numbers off
    # a spec sheet but guess at "which ThinkPad generation is this".
    conn = connect(tmp_path / "dealwatch.db")

    seed(conn, "v1|agree|0", "Lenovo ThinkPad T14 Gen 4 i5-1335U 16GB RAM 256GB SSD", 1000)
    seed(conn, "v1|disagree|0", "Lenovo ThinkPad T14 Gen 1 i5-1335U 16GB RAM 256GB SSD", 1001)

    output = run_report(PROFILE, conn, seed=1)

    assert "1 agree, 1 disagree" in output
    assert "extracted='1' implied='4'" in output
    assert "Gen 1 i5-1335U" in output  # the disagreeing title is named


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
