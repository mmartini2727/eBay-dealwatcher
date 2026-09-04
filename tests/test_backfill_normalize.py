"""Tests for scripts/backfill_normalize.py's run_backfill().

Real SQLite under tmp_path - no mocking the write path. No network.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import ProfileCompileError
from dealwatch.normalize.schema import MatchRule
from dealwatch.storage.sqlite import connect, record_sighting
from backfill_normalize import run_backfill

PROFILE = load_profile(Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml")


def seed(conn, item_id: str, title: str, seen_at: int, spec_status: str = "pending") -> None:
    raw = {
        "itemId": item_id,
        "title": title,
        "price": {"value": "349.99", "currency": "USD"},
        "buyingOptions": ["FIXED_PRICE"],
    }
    record_sighting(
        conn,
        item_id,
        dict(profile_id=PROFILE.id, title=title, spec_status=spec_status),
        dict(price_cents=34999, buying_options=["FIXED_PRICE"], raw_json=json.dumps(raw)),
        seen_at,
    )


def status_of(conn, item_id: str) -> str:
    return conn.execute(
        "SELECT spec_status FROM listings WHERE item_id = ?", (item_id,)
    ).fetchone()["spec_status"]


def test_backfill_normalizes_pending_listings(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "v1|1|0", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD", 1000)

    output = run_backfill(PROFILE, conn, all_listings=False)

    assert status_of(conn, "v1|1|0") == "ok"
    assert "ok" in output
    row = conn.execute(
        "SELECT spec_json, bucket_key FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row["spec_json"] is not None
    assert row["bucket_key"] is not None


def test_backfill_default_filter_skips_already_ok_listings(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "v1|1|0", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD", 1000)
    # Simulate an already-normalized row with a spec that would NOT match
    # what re-normalizing this title would actually produce - if the
    # default filter incorrectly touched 'ok' rows, this sentinel would be
    # overwritten and the test would catch it.
    conn.execute(
        "UPDATE listings SET spec_status = 'ok', bucket_key = 'SENTINEL' "
        "WHERE item_id = 'v1|1|0'"
    )

    run_backfill(PROFILE, conn, all_listings=False)

    row = conn.execute(
        "SELECT bucket_key FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row["bucket_key"] == "SENTINEL"


def test_backfill_all_reprocesses_regardless_of_status(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "v1|1|0", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD", 1000)
    conn.execute(
        "UPDATE listings SET spec_status = 'ok', bucket_key = 'SENTINEL' "
        "WHERE item_id = 'v1|1|0'"
    )

    run_backfill(PROFILE, conn, all_listings=True)

    row = conn.execute(
        "SELECT bucket_key FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row["bucket_key"] != "SENTINEL"
    assert status_of(conn, "v1|1|0") == "ok"


def test_backfill_run_twice_produces_identical_state(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "v1|1|0", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD", 1000)
    seed(conn, "v1|2|0", "Lenovo ThinkPad T14 Gen 1 16GB 256GB", 1001)  # -> partial
    seed(conn, "v1|3|0", "Lenovo ThinkPad T480 Core i5 16GB 256GB", 1002)  # -> not_target

    # Also cover the raw-fallback path (V0.7c) in the idempotency check -
    # not just the normally-mapped one above.
    bad_raw = {"itemId": "v1|4|0", "title": "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB 256GB"}
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES (?, ?, ?, 'pending', 1000, 1000, 0)",
        ("v1|4|0", PROFILE.id, bad_raw["title"]),
    )
    conn.execute(
        "INSERT INTO observations (item_id, observed_at, buying_options, raw_json) "
        "VALUES (?, ?, ?, ?)",
        ("v1|4|0", 1000, "[]", json.dumps(bad_raw)),
    )

    run_backfill(PROFILE, conn, all_listings=False)
    first = [dict(row) for row in conn.execute("SELECT * FROM listings ORDER BY item_id")]

    # Second run with the default filter finds nothing left pending/stale;
    # --all re-normalizes everything from scratch. Both must land on the
    # exact same state as the first run.
    run_backfill(PROFILE, conn, all_listings=False)
    second = [dict(row) for row in conn.execute("SELECT * FROM listings ORDER BY item_id")]
    assert first == second

    run_backfill(PROFILE, conn, all_listings=True)
    third = [dict(row) for row in conn.execute("SELECT * FROM listings ORDER BY item_id")]
    assert first == third


def test_backfill_leaves_a_listing_with_no_observations_untouched_and_counts_it(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # A listings row with no observations at all - shouldn't happen given
    # record_sighting always inserts one alongside the listing, but the
    # task requires this be handled explicitly rather than assumed away.
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES (?, ?, ?, 'pending', 1000, 1000, 0)",
        ("v1|orphan|0", PROFILE.id, "Lenovo ThinkPad T14 Gen 1 16GB 256GB"),
    )

    output = run_backfill(PROFILE, conn, all_listings=False)

    assert status_of(conn, "v1|orphan|0") == "pending"  # untouched, not flipped to anything
    assert "1 had no observations" in output


def test_backfill_fails_fast_on_a_bad_profile_even_with_nothing_selected(tmp_path):
    # Isolates the explicit up-front compile_profile() call from
    # normalize()'s own internal recompilation: seed a row that the
    # default filter will NOT select (already 'ok'), so the per-row loop
    # body - and therefore normalize() - never runs at all. If the
    # up-front check were removed, this profile's defect would go
    # completely unnoticed (an empty loop just returns a clean "0
    # processed" report) instead of raising. Sabotage-checked: removing
    # the explicit call left this test green, because a NON-empty
    # selection would still hit normalize()'s own compile_profile() on the
    # first row - fixed by using an empty selection here instead.
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "v1|1|0", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD", 1000)
    conn.execute("UPDATE listings SET spec_status = 'ok' WHERE item_id = 'v1|1|0'")

    bad_profile = PROFILE.model_copy(
        update={"reject": [MatchRule(id="bad", where="title", any=["(unclosed"], reason="t")]}
    )

    with pytest.raises(ProfileCompileError, match="invalid regex"):
        run_backfill(bad_profile, conn, all_listings=False)


def test_backfill_normalizes_a_mapping_failure_from_raw_fields_when_title_present(tmp_path):
    # V0.7c fix 1: a mapping-failure row with a title (the real case: 5
    # live rows, all missing `price`) is no longer left pending forever -
    # it gets normalized the same way the collector does, from the raw
    # dict's title/subtitle/condition_id directly.
    conn = connect(tmp_path / "dealwatch.db")
    bad_raw = {
        "itemId": "v1|bad|0",
        "title": "Lenovo ThinkPad T480 Core i5 16GB 256GB",  # no price
    }
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES (?, ?, ?, 'pending', 1000, 1000, 0)",
        ("v1|bad|0", PROFILE.id, bad_raw["title"]),
    )
    conn.execute(
        "INSERT INTO observations (item_id, observed_at, buying_options, raw_json) "
        "VALUES (?, ?, ?, ?)",
        ("v1|bad|0", 1000, "[]", json.dumps(bad_raw)),
    )

    output = run_backfill(PROFILE, conn, all_listings=False)

    # T480 fails the is-t14 require rule regardless of price - proves
    # normalize() actually ran against the raw-fallback fields, not just
    # that SOME status got written.
    assert status_of(conn, "v1|bad|0") == "not_target"
    assert "1 normalized from raw fields" in output


def test_backfill_reports_a_listing_with_no_title_as_unprocessable(tmp_path):
    # The genuinely unrecoverable case: no title anywhere to normalize
    # from (map_item_summary's other required fields, item_id/price, have
    # a fallback or don't matter here - title doesn't).
    conn = connect(tmp_path / "dealwatch.db")
    bad_raw = {"itemId": "v1|bad|0"}  # no title, no price
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES (?, ?, ?, 'pending', 1000, 1000, 0)",
        ("v1|bad|0", PROFILE.id, "(unknown)"),
    )
    conn.execute(
        "INSERT INTO observations (item_id, observed_at, buying_options, raw_json) "
        "VALUES (?, ?, ?, ?)",
        ("v1|bad|0", 1000, "[]", json.dumps(bad_raw)),
    )

    output = run_backfill(PROFILE, conn, all_listings=False)

    assert status_of(conn, "v1|bad|0") == "pending"  # untouched, not crashed on
    assert "1 had no title to normalize from" in output


def test_backfill_summary_distinguishes_mapped_from_raw_fallback(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "v1|1|0", "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD", 1000)

    bad_raw = {"itemId": "v1|2|0", "title": "Lenovo ThinkPad T480 Core i5 16GB 256GB"}
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES (?, ?, ?, 'pending', 1000, 1000, 0)",
        ("v1|2|0", PROFILE.id, bad_raw["title"]),
    )
    conn.execute(
        "INSERT INTO observations (item_id, observed_at, buying_options, raw_json) "
        "VALUES (?, ?, ?, ?)",
        ("v1|2|0", 1000, "[]", json.dumps(bad_raw)),
    )

    output = run_backfill(PROFILE, conn, all_listings=False)

    assert "1 mapped normally" in output
    assert "1 normalized from raw fields" in output
