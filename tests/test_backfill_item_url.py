"""Tests for scripts/backfill_item_url.py's run_backfill_item_url().

Real SQLite under tmp_path. No network.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dealwatch.storage.sqlite import connect, record_sighting
from backfill_item_url import run_backfill_item_url

PROFILE_ID = "thinkpad-t14"


def seed(conn, item_id, seen_at, *, url=None, raw_json_override=None):
    raw = {"itemId": item_id, "title": "t", "price": {"value": "100.00"}}
    if url is not None:
        raw["itemWebUrl"] = url
    record_sighting(
        conn,
        item_id,
        dict(profile_id=PROFILE_ID, title="t"),
        dict(price_cents=10000, raw_json=raw_json_override or json.dumps(raw)),
        seen_at,
    )


def test_updates_item_web_url_from_latest_raw_json(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "item-1", 1000, url="https://ebay.com/itm/1")

    output = run_backfill_item_url(conn)

    row = conn.execute("SELECT item_web_url FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["item_web_url"] == "https://ebay.com/itm/1"
    assert "updated=1" in output
    assert "skipped=0" in output
    assert "already_set=0" in output


def test_already_set_is_left_alone_and_counted_separately(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "item-1", 1000, url="https://ebay.com/itm/1")
    conn.execute(
        "UPDATE listings SET item_web_url = 'https://ebay.com/itm/DIFFERENT' WHERE item_id = 'item-1'"
    )

    output = run_backfill_item_url(conn)

    row = conn.execute("SELECT item_web_url FROM listings WHERE item_id = 'item-1'").fetchone()
    # Untouched - not re-derived from raw_json even though it would differ.
    assert row["item_web_url"] == "https://ebay.com/itm/DIFFERENT"
    assert "already_set=1" in output
    assert "updated=0" in output


def test_missing_item_web_url_in_raw_json_is_skipped(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "item-1", 1000, url=None)

    output = run_backfill_item_url(conn)

    row = conn.execute("SELECT item_web_url FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["item_web_url"] is None
    assert "skipped=1" in output


def test_unparseable_raw_json_is_skipped_not_raised(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "item-1", 1000, raw_json_override="{not valid json")

    output = run_backfill_item_url(conn)  # must not raise

    assert "skipped=1" in output


def test_listing_with_no_observations_is_skipped_not_crashed_on(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES (?, ?, ?, 'pending', 1000, 1000, 0)",
        ("orphan", PROFILE_ID, "t"),
    )

    output = run_backfill_item_url(conn)  # must not raise

    assert "skipped=1" in output


def test_run_twice_is_idempotent(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed(conn, "item-1", 1000, url="https://ebay.com/itm/1")

    run_backfill_item_url(conn)
    first_output = run_backfill_item_url(conn)

    assert "updated=0" in first_output
    assert "already_set=1" in first_output
