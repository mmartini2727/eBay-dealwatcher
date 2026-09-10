"""Tests for scripts/backfill_zero_lifespan.py's run_backfill().

Real SQLite under tmp_path. No network.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from dealwatch.storage.sqlite import connect, record_sighting
from backfill_zero_lifespan import run_backfill

PROFILE_ID = "thinkpad-t14"


def seed_listing(conn, item_id, *, first_seen, last_seen, gone_at, lifespan_mins):
    record_sighting(
        conn,
        item_id,
        dict(profile_id=PROFILE_ID, title="t"),
        dict(price_cents=10000, raw_json="{}"),
        first_seen,
    )
    conn.execute(
        "UPDATE listings SET last_seen = ?, gone_at = ?, lifespan_mins = ? "
        "WHERE item_id = ?",
        (last_seen, gone_at, lifespan_mins, item_id),
    )


def test_nulls_a_never_swept_zero_lifespan_row(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # last_seen == first_seen: never confirmed by a sweep, pre-V0.9b wrote 0.
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1000, gone_at=1000, lifespan_mins=0)

    output = run_backfill(conn, dry_run=False)

    row = conn.execute("SELECT lifespan_mins FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["lifespan_mins"] is None
    assert "1 row(s) set to NULL" in output


def test_leaves_a_genuine_zero_lifespan_row_untouched(tmp_path):
    # last_seen > first_seen: this WAS confirmed by a sweep and genuinely
    # died within the same minute - a real measurement, must survive.
    conn = connect(tmp_path / "dealwatch.db")
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1030, gone_at=1030, lifespan_mins=0)

    output = run_backfill(conn, dry_run=False)

    row = conn.execute("SELECT lifespan_mins FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["lifespan_mins"] == 0
    assert "0 row(s) set to NULL" in output


def test_leaves_a_nonzero_lifespan_row_untouched(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_listing(conn, "item-1", first_seen=1000, last_seen=5000, gone_at=5000, lifespan_mins=66)

    output = run_backfill(conn, dry_run=False)

    row = conn.execute("SELECT lifespan_mins FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["lifespan_mins"] == 66
    assert "0 row(s) set to NULL" in output


def test_leaves_an_already_null_lifespan_row_untouched(tmp_path):
    # A still-live listing, or one already backfilled - lifespan_mins IS
    # NULL doesn't match `= 0` in SQL, so this must not error or double-count.
    conn = connect(tmp_path / "dealwatch.db")
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1000, gone_at=None, lifespan_mins=None)

    output = run_backfill(conn, dry_run=False)

    row = conn.execute("SELECT lifespan_mins FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["lifespan_mins"] is None
    assert "0 row(s) set to NULL" in output


def test_dry_run_writes_nothing(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1000, gone_at=1000, lifespan_mins=0)

    output = run_backfill(conn, dry_run=True)

    row = conn.execute("SELECT lifespan_mins FROM listings WHERE item_id = 'item-1'").fetchone()
    assert row["lifespan_mins"] == 0  # untouched
    assert "1 row(s) would be set to NULL" in output
    assert "item-1" in output


def test_run_twice_is_idempotent(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    seed_listing(conn, "item-1", first_seen=1000, last_seen=1000, gone_at=1000, lifespan_mins=0)

    run_backfill(conn, dry_run=False)
    second_output = run_backfill(conn, dry_run=False)

    assert "0 row(s) set to NULL" in second_output
