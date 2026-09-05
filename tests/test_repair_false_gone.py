"""Tests for scripts/repair_false_gone.py's run_repair().

Real SQLite under tmp_path. No network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dealwatch.storage.sqlite import connect, record_sighting
from repair_false_gone import run_repair

PROFILE_ID = "thinkpad-t14"


def seed_listing(conn, item_id, *, first_seen, last_seen, gone_at, miss_count=3):
    record_sighting(
        conn,
        item_id,
        dict(profile_id=PROFILE_ID, title="t"),
        dict(price_cents=10000, raw_json="{}"),
        first_seen,
    )
    conn.execute(
        "UPDATE listings SET last_seen = ?, gone_at = ?, miss_count = ?, "
        "lifespan_mins = ? WHERE item_id = ?",
        (last_seen, gone_at, miss_count, (last_seen - first_seen) // 60 if gone_at else None, item_id),
    )


def test_clears_a_never_confirmed_row(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # Never confirmed by a sweep: last_seen == first_seen despite gone_at set.
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1000, gone_at=1000)

    output = run_repair(conn, dry_run=False)

    row = conn.execute(
        "SELECT gone_at, lifespan_mins, miss_count FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["gone_at"] is None
    assert row["lifespan_mins"] is None
    assert row["miss_count"] == 0
    assert "1 row(s) cleared" in output


def test_leaves_a_confirmed_row_untouched(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # Confirmed present by at least one sweep after first_seen: last_seen advanced.
    seed_listing(conn, "item-1", first_seen=1000, last_seen=5000, gone_at=5000)

    output = run_repair(conn, dry_run=False)

    row = conn.execute(
        "SELECT gone_at, lifespan_mins, miss_count FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["gone_at"] == 5000
    assert row["miss_count"] == 3
    assert "0 row(s) cleared" in output


def test_leaves_a_still_live_row_untouched(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # gone_at NULL - live listing, last_seen == first_seen just means it's brand new.
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1000, gone_at=None, miss_count=0)

    output = run_repair(conn, dry_run=False)

    row = conn.execute("SELECT gone_at FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["gone_at"] is None
    assert "0 row(s) cleared" in output


def test_dry_run_writes_nothing(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1000, gone_at=1000)

    output = run_repair(conn, dry_run=True)

    row = conn.execute("SELECT gone_at FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["gone_at"] == 1000  # untouched
    assert "1 row(s) would be cleared" in output
    assert "item-1" in output


def test_run_twice_is_idempotent(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1000, gone_at=1000)

    run_repair(conn, dry_run=False)
    second_output = run_repair(conn, dry_run=False)

    assert "0 row(s) cleared" in second_output
