"""Tests for the listing/observation write path in dealwatch.storage.sqlite
(design.md §4.1, §4.2).

Real SQLite files under tmp_path - no mocking the persistence layer, since
persistence and the disappearance bookkeeping are the entire point of this
milestone. No network.
"""

import json
import logging

from dealwatch.normalize.engine import SpecResult
from dealwatch.storage.sqlite import (
    MISS_THRESHOLD,
    connect,
    count_active_listings,
    get_latest_observation,
    get_observations,
    record_sighting,
    record_sweep,
    store_spec,
)

PROFILE_ID = "thinkpad-t14"


def make_conn(tmp_path):
    return connect(tmp_path / "dealwatch.db")


def listing_fields(**overrides) -> dict:
    base = dict(
        profile_id=PROFILE_ID,
        title="Lenovo ThinkPad T14 Gen 1 16GB 256GB",
        seller="refurb_liquidators",
        seller_feedback_pct=99.5,
        seller_feedback_score=40213,
        condition_id=3000,
    )
    base.update(overrides)
    return base


def observation_fields(**overrides) -> dict:
    base = dict(
        price_cents=34999,
        shipping_cents=1250,
        total_cents=36249,
        buying_options=["FIXED_PRICE"],
        current_bid_cents=None,
        bid_count=None,
        raw_json='{"itemId": "v1|1|0"}',
    )
    base.update(overrides)
    return base


def sight(conn, item_id, seen_at, **overrides):
    lf = {k: v for k, v in overrides.items() if k in listing_fields()}
    of = {k: v for k, v in overrides.items() if k in observation_fields()}
    record_sighting(
        conn, item_id, listing_fields(**lf), observation_fields(**of), seen_at
    )


def test_unchanged_sighting_writes_exactly_one_observation(tmp_path):
    conn = make_conn(tmp_path)

    sight(conn, "item-1", 1000)
    sight(conn, "item-1", 1300)  # identical fields, three polls later
    sight(conn, "item-1", 1600)

    observations = get_observations(conn, "item-1")
    assert len(observations) == 1
    assert observations[0]["price_cents"] == 34999


def test_price_change_writes_two_observations_and_preserves_the_old_one(tmp_path):
    conn = make_conn(tmp_path)

    sight(conn, "item-1", 1000, price_cents=34999, total_cents=36249)
    sight(conn, "item-1", 2000, price_cents=29999, total_cents=31249)

    observations = get_observations(conn, "item-1")
    assert len(observations) == 2
    assert observations[0]["price_cents"] == 34999
    assert observations[1]["price_cents"] == 29999

    latest = get_latest_observation(conn, "item-1")
    assert latest["price_cents"] == 29999


def test_shipping_null_present_null_each_transition_writes_one_observation(tmp_path):
    conn = make_conn(tmp_path)

    sight(conn, "item-1", 1000, shipping_cents=None, total_cents=None)  # unknown
    sight(conn, "item-1", 1300, shipping_cents=None, total_cents=None)  # unchanged
    sight(conn, "item-1", 1600, shipping_cents=1250, total_cents=36249)  # resolved
    sight(conn, "item-1", 1900, shipping_cents=1250, total_cents=36249)  # unchanged
    sight(conn, "item-1", 2200, shipping_cents=None, total_cents=None)  # back to unknown

    observations = get_observations(conn, "item-1")
    # One at first sight, one for NULL->1250, one for 1250->NULL. The two
    # "unchanged" polls in between must not add rows.
    assert len(observations) == 3
    assert [o["shipping_cents"] for o in observations] == [None, 1250, None]


def test_title_change_writes_observation_and_marks_spec_stale(tmp_path):
    conn = make_conn(tmp_path)

    sight(conn, "item-1", 1000, title="Lenovo ThinkPad T14 Gen 1 16GB 256GB")

    # Simulate V0.7 having already normalized this listing, so we can prove
    # the title change actually resets it rather than it starting that way.
    conn.execute(
        "UPDATE listings SET spec_json = '{}', bucket_key = 'gen1|i5|16|256', "
        "spec_status = 'ok' WHERE item_id = 'item-1'"
    )

    sight(conn, "item-1", 2000, title="Lenovo ThinkPad T14 Gen 2 16GB 512GB")

    observations = get_observations(conn, "item-1")
    assert len(observations) == 2

    row = conn.execute(
        "SELECT title, spec_json, bucket_key, spec_status FROM listings "
        "WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["title"] == "Lenovo ThinkPad T14 Gen 2 16GB 512GB"
    assert row["spec_status"] == "stale"
    assert row["spec_json"] is None
    assert row["bucket_key"] is None


def test_insert_with_explicit_spec_status_pending_stores_pending(tmp_path):
    conn = make_conn(tmp_path)

    record_sighting(
        conn,
        "item-1",
        listing_fields(spec_status="pending"),
        observation_fields(),
        1000,
    )

    row = conn.execute(
        "SELECT spec_status FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["spec_status"] == "pending"


def test_insert_with_spec_status_omitted_defaults_to_pending(tmp_path):
    conn = make_conn(tmp_path)

    # listing_fields() here carries no spec_status key at all - the default
    # must come from record_sighting, not from a value the test supplied.
    record_sighting(conn, "item-1", listing_fields(), observation_fields(), 1000)

    row = conn.execute(
        "SELECT spec_status FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["spec_status"] == "pending"


def test_insert_with_spec_status_ok_is_not_overridden(tmp_path):
    conn = make_conn(tmp_path)

    record_sighting(
        conn, "item-1", listing_fields(spec_status="ok"), observation_fields(), 1000
    )

    row = conn.execute(
        "SELECT spec_status FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["spec_status"] == "ok"


def test_fast_poll_sighting_does_not_advance_last_seen(tmp_path):
    conn = make_conn(tmp_path)

    sight(conn, "item-1", 1000)
    before = conn.execute(
        "SELECT last_seen FROM listings WHERE item_id = 'item-1'"
    ).fetchone()["last_seen"]

    sight(conn, "item-1", 999_999)  # a much later fast-poll sighting

    after = conn.execute(
        "SELECT last_seen FROM listings WHERE item_id = 'item-1'"
    ).fetchone()["last_seen"]

    assert after == before == 1000


def test_sweep_marks_a_missing_item_gone_at_threshold(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    record_sweep(conn, ["item-1"], PROFILE_ID, swept_at=1000)  # establishes last_seen

    record_sweep(conn, [], PROFILE_ID, swept_at=2000)  # miss 1
    row = conn.execute(
        "SELECT miss_count, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["miss_count"] == 1
    assert row["gone_at"] is None

    record_sweep(conn, [], PROFILE_ID, swept_at=3000)  # miss 2
    row = conn.execute(
        "SELECT miss_count, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["miss_count"] == 2
    assert row["gone_at"] is None

    record_sweep(conn, [], PROFILE_ID, swept_at=4000)  # miss 3 -> gone
    row = conn.execute(
        "SELECT miss_count, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["miss_count"] == MISS_THRESHOLD
    assert row["gone_at"] is not None


def test_gone_at_equals_last_seen_not_swept_at(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    record_sweep(conn, ["item-1"], PROFILE_ID, swept_at=5000)  # last_seen = 5000

    record_sweep(conn, [], PROFILE_ID, swept_at=6000)
    record_sweep(conn, [], PROFILE_ID, swept_at=7000)
    record_sweep(conn, [], PROFILE_ID, swept_at=8000)  # 3rd miss, swept_at=8000

    row = conn.execute(
        "SELECT last_seen, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["gone_at"] == row["last_seen"] == 5000
    assert row["gone_at"] != 8000


def test_miss_then_sighting_resets_miss_count_and_never_sets_gone_at(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    record_sweep(conn, ["item-1"], PROFILE_ID, swept_at=1000)

    record_sweep(conn, [], PROFILE_ID, swept_at=2000)  # miss 1
    record_sweep(conn, [], PROFILE_ID, swept_at=3000)  # miss 2

    sight(conn, "item-1", 3500)  # a poll sees it again before the 3rd sweep miss

    row = conn.execute(
        "SELECT miss_count, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["miss_count"] == 0
    assert row["gone_at"] is None

    # And a subsequent miss starts counting from 0, not resuming at 2.
    record_sweep(conn, [], PROFILE_ID, swept_at=4000)
    row = conn.execute(
        "SELECT miss_count, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["miss_count"] == 1
    assert row["gone_at"] is None


def test_resurrection_clears_gone_at_and_lifespan(tmp_path, caplog):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    record_sweep(conn, ["item-1"], PROFILE_ID, swept_at=1000)
    record_sweep(conn, [], PROFILE_ID, swept_at=2000)
    record_sweep(conn, [], PROFILE_ID, swept_at=3000)
    record_sweep(conn, [], PROFILE_ID, swept_at=4000)  # gone

    row = conn.execute(
        "SELECT gone_at, lifespan_mins FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["gone_at"] is not None
    assert row["lifespan_mins"] is not None

    with caplog.at_level(logging.WARNING):
        sight(conn, "item-1", 5000)  # eBay's index inconsistency, not a real relist

    row = conn.execute(
        "SELECT gone_at, lifespan_mins, miss_count FROM listings "
        "WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["gone_at"] is None
    assert row["lifespan_mins"] is None
    assert row["miss_count"] == 0
    assert any(
        record.levelname == "WARNING" and "item-1" in record.message
        for record in caplog.records
    )


def test_count_active_listings_excludes_gone_items(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    sight(conn, "item-2", 1000)
    record_sweep(conn, ["item-1", "item-2"], PROFILE_ID, swept_at=1000)

    assert count_active_listings(conn, PROFILE_ID) == 2

    record_sweep(conn, ["item-2"], PROFILE_ID, swept_at=2000)  # item-1 missed
    record_sweep(conn, ["item-2"], PROFILE_ID, swept_at=3000)
    record_sweep(conn, ["item-2"], PROFILE_ID, swept_at=4000)  # item-1 gone

    assert count_active_listings(conn, PROFILE_ID) == 1


def test_store_spec_writes_all_four_fields(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)

    result = SpecResult(
        spec={"generation": "1", "cpu_family": "intel-10th"},
        spec_status="ok",
        reject_rule_id=None,
        bucket_key="1|intel-10th|16|256",
    )
    store_spec(conn, "item-1", result)

    row = conn.execute(
        "SELECT spec_json, bucket_key, spec_status, reject_rule_id "
        "FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert json.loads(row["spec_json"]) == {"generation": "1", "cpu_family": "intel-10th"}
    assert row["bucket_key"] == "1|intel-10th|16|256"
    assert row["spec_status"] == "ok"
    assert row["reject_rule_id"] is None


def test_store_spec_writes_reject_rule_id_when_rejected(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)

    result = SpecResult(spec={}, spec_status="rejected", reject_rule_id="lot-listing", bucket_key=None)
    store_spec(conn, "item-1", result)

    row = conn.execute(
        "SELECT spec_json, bucket_key, spec_status, reject_rule_id "
        "FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    # The empty dict a rejected result carries is a real recorded fact
    # ("normalize ran and stopped here"), not the same thing as never
    # having run at all - so this is '{}', not NULL.
    assert row["spec_json"] == "{}"
    assert row["bucket_key"] is None
    assert row["spec_status"] == "rejected"
    assert row["reject_rule_id"] == "lot-listing"


def test_store_spec_is_idempotent(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    result = SpecResult(
        spec={"generation": "2"}, spec_status="partial", reject_rule_id=None, bucket_key="2|?|?|?"
    )

    store_spec(conn, "item-1", result)
    first = dict(conn.execute("SELECT * FROM listings WHERE item_id = 'item-1'").fetchone())

    store_spec(conn, "item-1", result)
    second = dict(conn.execute("SELECT * FROM listings WHERE item_id = 'item-1'").fetchone())

    assert first == second


def test_migration_runs_twice_cleanly_and_leaves_budget_intact(tmp_path):
    db_path = tmp_path / "dealwatch.db"
    conn = connect(db_path)
    conn.execute("INSERT INTO budget (id, period, used) VALUES (1, '2026-08-30', 42)")
    conn.close()

    conn2 = connect(db_path)  # re-runs migrations against the same file
    row = conn2.execute("SELECT period, used FROM budget WHERE id = 1").fetchone()
    assert row["period"] == "2026-08-30"
    assert row["used"] == 42

    # And the new tables are genuinely usable, not just present.
    sight(conn2, "item-1", 1000)
    assert get_latest_observation(conn2, "item-1") is not None
