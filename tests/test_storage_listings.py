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
    last_alert,
    record_alert,
    record_sighting,
    record_sweep,
    store_sanity_flags,
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
        item_web_url=None,
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


def test_item_web_url_is_set_on_insert(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000, item_web_url="https://ebay.com/itm/1")

    row = conn.execute(
        "SELECT item_web_url FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["item_web_url"] == "https://ebay.com/itm/1"


def test_item_web_url_self_heals_on_a_later_sighting(tmp_path):
    # V0.9 regression: item_web_url used to be consulted only on INSERT,
    # so a row created before that fix (or by a caller that genuinely had
    # no URL yet) would carry a NULL item_web_url forever - no sighting
    # after the first one would ever touch it, and only a one-time backfill
    # script (scripts/backfill_item_url.py) could repair it. The UPDATE
    # branch now sets it too, so a later sighting that DOES have a URL
    # heals the row without any backfill.
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)  # no item_web_url - simulates a pre-fix row
    row = conn.execute(
        "SELECT item_web_url FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["item_web_url"] is None

    sight(conn, "item-1", 2000, item_web_url="https://ebay.com/itm/1")

    row = conn.execute(
        "SELECT item_web_url FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["item_web_url"] == "https://ebay.com/itm/1"


def test_item_web_url_update_does_not_clobber_an_existing_value_with_none(tmp_path):
    # The COALESCE on the UPDATE branch exists specifically for this case:
    # a later sighting that doesn't have a URL to hand (e.g. the raw-only
    # mapping-failure path, if itemWebUrl itself happened to be absent from
    # that particular raw dict) must not overwrite an already-good value.
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000, item_web_url="https://ebay.com/itm/1")

    sight(conn, "item-1", 2000, item_web_url=None)  # e.g. a poll with no URL

    row = conn.execute(
        "SELECT item_web_url FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["item_web_url"] == "https://ebay.com/itm/1"  # untouched, not wiped


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


def test_miss_threshold_is_pinned_to_5():
    # The guard: 5 is an empirical derivation (see storage/sqlite.py's
    # comment on MISS_THRESHOLD), not an arbitrary number, and every other
    # test in this file is written against the constant so it keeps
    # testing real behavior if this ever changes again - which means
    # nothing else here would notice a silent drift to, say, 3. This is
    # the one place that hardcodes the literal.
    assert MISS_THRESHOLD == 5


def test_n_minus_one_misses_leaves_gone_at_null_nth_miss_sets_it(tmp_path):
    # Written against MISS_THRESHOLD so it keeps testing the real boundary
    # if the constant changes again - but this is NOT "loop MISS_THRESHOLD
    # times and check the end state," which would pass for any threshold
    # including 1 without ever proving the off-by-one boundary is right.
    # Checking N-1 (still NULL) immediately before N (now set) is what
    # actually exercises that boundary.
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    record_sweep(conn, ["item-1"], PROFILE_ID, swept_at=1000)  # establishes last_seen

    swept_at = 1000
    for _ in range(MISS_THRESHOLD - 1):
        swept_at += 1000
        record_sweep(conn, [], PROFILE_ID, swept_at=swept_at)

    row = conn.execute(
        "SELECT miss_count, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["miss_count"] == MISS_THRESHOLD - 1
    assert row["gone_at"] is None

    swept_at += 1000
    record_sweep(conn, [], PROFILE_ID, swept_at=swept_at)  # the Nth miss

    row = conn.execute(
        "SELECT miss_count, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["miss_count"] == MISS_THRESHOLD
    assert row["gone_at"] is not None


def test_gone_at_equals_last_seen_not_swept_at(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    record_sweep(conn, ["item-1"], PROFILE_ID, swept_at=5000)  # last_seen = 5000

    # MISS_THRESHOLD-many misses to reach "gone" - this test isn't about the
    # threshold value itself (see test_n_minus_one_misses_... for that), just
    # about gone_at being last_seen rather than whichever swept_at got there.
    swept_at = 5000
    for _ in range(MISS_THRESHOLD):
        swept_at += 1000
        record_sweep(conn, [], PROFILE_ID, swept_at=swept_at)

    row = conn.execute(
        "SELECT last_seen, gone_at FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["gone_at"] == row["last_seen"] == 5000
    assert row["gone_at"] != swept_at


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
    swept_at = 1000
    for _ in range(MISS_THRESHOLD):  # MISS_THRESHOLD-many misses -> gone
        swept_at += 1000
        record_sweep(conn, [], PROFILE_ID, swept_at=swept_at)

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

    # MISS_THRESHOLD-many misses for item-1 (item-2 keeps being seen so it
    # stays active) - this test is about the active-count filter, not the
    # threshold value itself.
    swept_at = 1000
    for _ in range(MISS_THRESHOLD):
        swept_at += 1000
        record_sweep(conn, ["item-2"], PROFILE_ID, swept_at=swept_at)  # item-1 missed

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


def test_store_sanity_flags_sets_exactly_the_given_flags_and_leaves_others_untouched(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    sight(conn, "item-2", 1000)
    sight(conn, "item-3", 1000)
    # A pre-existing flag on a row NOT included in this batch must survive -
    # store_sanity_flags only touches the rows it's handed, not every row.
    conn.execute("UPDATE listings SET sanity_flagged = 1 WHERE item_id = 'item-3'")

    store_sanity_flags(conn, [("item-1", True), ("item-2", False)])

    flags = {
        row["item_id"]: row["sanity_flagged"]
        for row in conn.execute("SELECT item_id, sanity_flagged FROM listings").fetchall()
    }
    assert flags["item-1"] == 1
    assert flags["item-2"] == 0
    assert flags["item-3"] == 1  # untouched, not reset to NULL/0


def test_store_sanity_flags_empty_list_is_a_noop_not_an_error(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)

    store_sanity_flags(conn, [])  # must not raise

    row = conn.execute(
        "SELECT sanity_flagged FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["sanity_flagged"] is None  # never touched, still the column default


def test_store_sanity_flags_empty_list_does_not_take_a_write_lock(tmp_path):
    # The test above (data untouched, no exception) also passes if an
    # empty batch opens a transaction and runs an empty executemany inside
    # it - that's harmless too, so it doesn't actually prove no lock was
    # taken. This test does: it holds the write lock on a second
    # connection first, the way the live collector's poll/sweep would, and
    # asserts an empty call returns immediately rather than blocking on
    # busy_timeout for a lock it never needed - the exact "database is
    # locked" failure mode this milestone exists to prevent.
    db_path = tmp_path / "dealwatch.db"
    conn = connect(db_path)
    sight(conn, "item-1", 1000)
    conn.execute("PRAGMA busy_timeout=200")  # fail fast if this regresses

    blocker = connect(db_path)
    blocker.execute("BEGIN IMMEDIATE")
    try:
        store_sanity_flags(conn, [])  # must not attempt to acquire the lock
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


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


def test_migration_4_applies_to_a_database_already_at_version_3(tmp_path, monkeypatch):
    # The production path is the upgrade path, not a fresh file: every real
    # database this runs against already has months of history at whatever
    # version it was last opened with. Builds a genuine v3 database using
    # the REAL migrations 1-3 (sliced from the actual _MIGRATIONS, not a
    # hand-copied duplicate that could silently drift from it), then
    # reconnects with the full, current _MIGRATIONS and confirms migration
    # 4 actually applies - and that existing data survives the upgrade.
    import dealwatch.storage.sqlite as storage_module

    db_path = tmp_path / "dealwatch.db"
    real_migrations = storage_module._MIGRATIONS
    v3_only = [m for m in real_migrations if m[0] <= 3]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", v3_only)

    conn = storage_module.connect(db_path)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 3
    # Raw SQL matching v3's actual shape, not sight()/record_sighting():
    # that helper is written for the CURRENT schema (it references
    # variation_id, added at v5) and would fail against a database that is
    # deliberately older than that - the same reason this test exists.
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES "
        "('item-1', ?, 'Lenovo ThinkPad T14 Gen 1 16GB 256GB', 'pending', 1000, 1000, 0)",
        (PROFILE_ID,),
    )
    conn.close()

    # Scoped to v4, not the real (now v5) _MIGRATIONS - keeps this test's
    # assertions about "migration 4 applied" true regardless of what later
    # migrations exist. See test_migration_5_applies_to_a_database_already_
    # at_version_4 for the v4->v5 step.
    v4_only = [m for m in real_migrations if m[0] <= 4]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", v4_only)

    upgraded = storage_module.connect(db_path)  # the actual upgrade path
    version = upgraded.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 4

    # Pre-existing data survived the upgrade...
    row = upgraded.execute(
        "SELECT title FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["title"] == "Lenovo ThinkPad T14 Gen 1 16GB 256GB"

    # ...and the new column is real and usable, not just present.
    upgraded.execute(
        "UPDATE listings SET sanity_flagged = 1 WHERE item_id = 'item-1'"
    )
    flagged = upgraded.execute(
        "SELECT sanity_flagged FROM listings WHERE item_id = 'item-1'"
    ).fetchone()
    assert flagged["sanity_flagged"] == 1


def test_migration_5_applies_to_a_database_already_at_version_4(tmp_path, monkeypatch):
    # Same production-is-the-upgrade-path reasoning as the v3->v4 test
    # above. Confirms migration 5's variation_id backfill matches
    # parse_variation_id()'s Python rule, the sweeps table is real and
    # usable, and re-running migrations against an already-v5 file is a
    # true no-op - not just "doesn't error," but leaves the backfilled
    # value and the sweeps row byte-identical.
    #
    # Scoped to <= 5, not the real (now v6) _MIGRATIONS - same reasoning as
    # the v3->v4 test's v4_only slice: keeps this test's "migration 5
    # applied" assertions true regardless of what later migrations exist.
    # See test_migration_6_applies_to_a_database_already_at_version_5 for
    # the v5->v6 step.
    import dealwatch.storage.sqlite as storage_module

    db_path = tmp_path / "dealwatch.db"
    real_migrations = storage_module._MIGRATIONS
    v4_only = [m for m in real_migrations if m[0] <= 4]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", v4_only)

    conn = storage_module.connect(db_path)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 4
    # v4 has no variation_id column yet - raw SQL, not record_sighting().
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES "
        "('v1|123|456', ?, 't', 'ok', 1000, 1000, 0)",
        (PROFILE_ID,),
    )
    conn.close()

    v5_only = [m for m in real_migrations if m[0] <= 5]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", v5_only)

    upgraded = storage_module.connect(db_path)
    version = upgraded.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 5

    # The backfill computed variation_id from item_id, matching
    # parse_variation_id("v1|123|456") == "456" exactly.
    row = upgraded.execute(
        "SELECT variation_id FROM listings WHERE item_id = 'v1|123|456'"
    ).fetchone()
    assert row["variation_id"] == "456"

    # sweeps is a real, usable table, not just present.
    upgraded.execute(
        "INSERT INTO sweeps (profile_id, swept_at, fetched_count, "
        "distinct_count, active_count_before, truncated, sweep_recorded) "
        "VALUES (?, 1000, 10, 9, 8, 0, 1)",
        (PROFILE_ID,),
    )
    assert upgraded.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0] == 1
    upgraded.close()

    # Re-running migrations against an already-v5 file is a no-op: the
    # backfilled value and the sweeps row are untouched, not recomputed or
    # duplicated.
    reconnected = storage_module.connect(db_path)
    version = reconnected.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 5
    row = reconnected.execute(
        "SELECT variation_id FROM listings WHERE item_id = 'v1|123|456'"
    ).fetchone()
    assert row["variation_id"] == "456"
    assert reconnected.execute("SELECT COUNT(*) FROM sweeps").fetchone()[0] == 1


def test_migration_6_applies_to_a_database_already_at_version_5(tmp_path, monkeypatch):
    # Same production-is-the-upgrade-path reasoning as the migration 5 test
    # above. Confirms the alerts table + its index are real and usable, and
    # a second connect() against an already-v6 file is a true no-op.
    #
    # Pinned to v6_only rather than the real (now v7+) _MIGRATIONS list, on
    # purpose - this test's claim is specifically about migration 6, and a
    # literal pin is what makes that claim keep meaning something once a
    # later migration exists (see the migration 7 test below, and CLAUDE.md
    # on why a pin test must assert a literal, not "whatever's latest").
    import dealwatch.storage.sqlite as storage_module

    db_path = tmp_path / "dealwatch.db"
    real_migrations = storage_module._MIGRATIONS
    v5_only = [m for m in real_migrations if m[0] <= 5]
    v6_only = [m for m in real_migrations if m[0] <= 6]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", v5_only)

    conn = storage_module.connect(db_path)
    version = conn.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 5
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES "
        "('item-1', ?, 't', 'ok', 1000, 1000, 0)",
        (PROFILE_ID,),
    )
    conn.close()

    monkeypatch.setattr(storage_module, "_MIGRATIONS", v6_only)

    upgraded = storage_module.connect(db_path)
    version = upgraded.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 6

    upgraded.execute(
        "INSERT INTO alerts (item_id, profile_id, sent_at, dry_run, "
        "price_cents, price_is_price_only, bucket_key, baseline_layer, "
        "baseline_match, baseline_n, baseline_p25_cents, baseline_p50_cents, "
        "ratio_to_p25, sanity_flagged, delivery_status) VALUES "
        "('item-1', ?, 1000, 1, 20000, 0, 'bucket', 'seed', '{}', NULL, "
        "20000, 25000, 1.0, 0, 'dry_run')",
        (PROFILE_ID,),
    )
    assert upgraded.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1
    upgraded.close()

    reconnected = storage_module.connect(db_path)
    version = reconnected.execute("SELECT version FROM schema_version").fetchone()[0]
    assert version == 6
    assert reconnected.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 1


def test_migration_7_applies_to_a_database_already_at_version_6(tmp_path, monkeypatch):
    # V0.9a: notifier column + its default backfill, and the replaced
    # index. Same "start pinned to the prior version, then let the real
    # migration list run" shape as the migration 6 test above.
    import dealwatch.storage.sqlite as storage_module

    db_path = tmp_path / "dealwatch.db"
    real_migrations = storage_module._MIGRATIONS
    v6_only = [m for m in real_migrations if m[0] <= 6]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", v6_only)

    conn = storage_module.connect(db_path)
    assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 6
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "first_seen, last_seen, miss_count) VALUES "
        "('item-1', ?, 't', 'ok', 1000, 1000, 0)",
        (PROFILE_ID,),
    )
    # A pre-V0.9a INSERT - no notifier column exists yet at v6, so this is
    # exactly the shape every row in a real pre-upgrade database has.
    conn.execute(
        "INSERT INTO alerts (item_id, profile_id, sent_at, dry_run, "
        "price_cents, price_is_price_only, bucket_key, baseline_layer, "
        "baseline_match, baseline_n, baseline_p25_cents, baseline_p50_cents, "
        "ratio_to_p25, sanity_flagged, delivery_status) VALUES "
        "('item-1', ?, 1000, 1, 20000, 0, 'bucket', 'seed', '{}', NULL, "
        "20000, 25000, 1.0, 0, 'dry_run')",
        (PROFILE_ID,),
    )
    conn.close()

    monkeypatch.setattr(storage_module, "_MIGRATIONS", real_migrations)

    upgraded = storage_module.connect(db_path)
    assert upgraded.execute("SELECT version FROM schema_version").fetchone()[0] == 7

    # The pre-existing row backfills to 'discord' - it predates any
    # notifier but IS a Discord send, since Discord was the only notifier
    # that ever existed before this migration.
    row = upgraded.execute(
        "SELECT notifier FROM alerts WHERE item_id = 'item-1'"
    ).fetchone()
    assert row["notifier"] == "discord"

    # A new row can specify a different notifier explicitly.
    upgraded.execute(
        "INSERT INTO alerts (item_id, profile_id, sent_at, dry_run, "
        "price_cents, price_is_price_only, bucket_key, baseline_layer, "
        "baseline_match, baseline_n, baseline_p25_cents, baseline_p50_cents, "
        "ratio_to_p25, sanity_flagged, delivery_status, notifier) VALUES "
        "('item-1', ?, 2000, 1, 19000, 0, 'bucket', 'seed', '{}', NULL, "
        "20000, 25000, 0.95, 0, 'dry_run', 'pushover')",
        (PROFILE_ID,),
    )
    assert upgraded.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2
    upgraded.close()

    reconnected = storage_module.connect(db_path)
    assert reconnected.execute("SELECT version FROM schema_version").fetchone()[0] == 7
    assert reconnected.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 2


# Every shape parse_variation_id() itself is required to handle correctly
# (test_listing.py's coverage test), plus a couple more that specifically
# stress the SQL backfill's string/position arithmetic: "" and "00" are the
# two easiest ways for instr()/substr() off-by-ones to diverge from Python's
# split()/comparison without either producing an obvious crash.
_MIGRATION_5_BACKFILL_SHAPES = [
    "v1|123|0",       # exact "0" -> not a variation -> None
    "v1|123|456",     # ordinary variation -> "456"
    "v1|123",         # only 2 parts -> None
    "v1|123|",        # empty 3rd part -> None
    "v1|123|456|789", # 4 parts -> None
    "v1||456",        # empty 2nd part, but still exactly 3 parts -> "456"
    "",                # empty string entirely -> None
    "garbage",         # no pipes at all -> None
    "a|b|c",           # doesn't start with v1 - shape only, not the prefix -> "c"
    "v1|123|00",       # "00" != "0" as a string -> "00", not None
]


def test_migration_5_backfill_matches_parse_variation_id_across_item_id_shapes(
    tmp_path, monkeypatch
):
    # test_migration_5_applies_to_a_database_already_at_version_4 only
    # proves the backfill isn't completely broken on one ordinary shape.
    # The SQL is hand-written nested instr()/substr() with no split()
    # available in SQLite (see migration 5's comment) - the empty-segment
    # and off-by-one cases above are exactly where that kind of SQL and
    # Python's str.split() are most likely to quietly disagree. This seeds
    # one row per shape and asserts the SQL backfill agrees with
    # parse_variation_id() exactly, case by case, not just on the shape
    # someone happened to hand-verify while writing the migration.
    import dealwatch.storage.sqlite as storage_module
    from dealwatch.normalize.listing import parse_variation_id

    db_path = tmp_path / "dealwatch.db"
    real_migrations = storage_module._MIGRATIONS
    v4_only = [m for m in real_migrations if m[0] <= 4]
    monkeypatch.setattr(storage_module, "_MIGRATIONS", v4_only)

    conn = storage_module.connect(db_path)
    for i, item_id in enumerate(_MIGRATION_5_BACKFILL_SHAPES):
        conn.execute(
            "INSERT INTO listings (item_id, profile_id, title, spec_status, "
            "first_seen, last_seen, miss_count) VALUES (?, ?, 't', 'ok', 1000, 1000, 0)",
            (item_id, f"{PROFILE_ID}-{i}"),
        )
    conn.close()

    monkeypatch.setattr(storage_module, "_MIGRATIONS", real_migrations)
    upgraded = storage_module.connect(db_path)

    for item_id in _MIGRATION_5_BACKFILL_SHAPES:
        row = upgraded.execute(
            "SELECT variation_id FROM listings WHERE item_id = ?", (item_id,)
        ).fetchone()
        assert row["variation_id"] == parse_variation_id(item_id), repr(item_id)


# ---------------------------------------------------------------------------
# record_alert / last_alert (V0.9, design.md's dated entry)
# ---------------------------------------------------------------------------


def _alert_kwargs(**overrides):
    defaults = dict(
        dry_run=False,
        price_cents=9000,
        price_is_price_only=False,
        bucket_key="1|intel-10th|16",
        baseline_layer="seed",
        baseline_match="{}",
        baseline_n=None,
        baseline_p25_cents=10000,
        baseline_p50_cents=15000,
        ratio_to_p25=0.9,
        sanity_flagged=False,
        delivery_status="sent",
    )
    defaults.update(overrides)
    return defaults


def test_last_alert_returns_none_when_no_alert_exists(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    assert last_alert(conn, "item-1") is None


def test_record_alert_then_last_alert_round_trips(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)

    record_alert(conn, "item-1", PROFILE_ID, 2000, **_alert_kwargs(price_cents=9000))

    row = last_alert(conn, "item-1")
    assert row is not None
    assert row["sent_at"] == 2000
    assert row["price_cents"] == 9000
    assert row["dry_run"] == 0
    assert row["delivery_status"] == "sent"


def test_last_alert_ignores_dry_run_returns_most_recent_regardless(tmp_path):
    # Load-bearing (storage/sqlite.py's docstring): a dry-run row must gate
    # cooldown/re-alert exactly like a real one - this is what lets a
    # dry-run day double as the first-alert-ever guard.
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)

    record_alert(conn, "item-1", PROFILE_ID, 2000, **_alert_kwargs(dry_run=True, delivery_status="dry_run"))

    row = last_alert(conn, "item-1")
    assert row is not None
    assert row["dry_run"] == 1


def test_last_alert_returns_the_most_recent_by_sent_at(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)

    record_alert(conn, "item-1", PROFILE_ID, 2000, **_alert_kwargs(price_cents=9000))
    record_alert(conn, "item-1", PROFILE_ID, 3000, **_alert_kwargs(price_cents=8500))

    row = last_alert(conn, "item-1")
    assert row["sent_at"] == 3000
    assert row["price_cents"] == 8500


def test_record_alert_defaults_notifier_to_discord(tmp_path):
    # Every pre-V0.9a call site (this file's other tests included) doesn't
    # pass notifier at all - it must keep meaning "discord", not become a
    # TypeError or silently write something else.
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)

    record_alert(conn, "item-1", PROFILE_ID, 2000, **_alert_kwargs())

    row = last_alert(conn, "item-1")
    assert row["notifier"] == "discord"


def test_last_alert_dedups_across_notifiers_not_per_notifier(tmp_path):
    # V0.9a's central risk (design.md's dated entry): if last_alert() only
    # saw rows for one notifier, a Pushover outage recovering mid-cycle
    # would find no PRIOR pushover row for an item Discord already alerted
    # on, and re-fire on the very next cycle even though the user already
    # got the Discord alert. Dedup must be "was this item alerted on
    # recently, at all" - not per-channel.
    #
    # Deliberately tagged 'pushover', not 'discord' - a row tagged with
    # whatever the CALLER's own notifier happens to be would pass even a
    # broken, notifier-filtered last_alert() by coincidence, and prove
    # nothing (this project has hit that exact false-confidence shape
    # before, in the cooldown-boundary sabotage - CLAUDE.md/design.md's
    # dated entries). Tagging it 'pushover' and then querying with no
    # notifier argument at all - because the real API doesn't take one -
    # is what actually exercises "found regardless of which channel wrote
    # it."
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)

    record_alert(
        conn, "item-1", PROFILE_ID, 2000,
        **_alert_kwargs(price_cents=9000, notifier="pushover"),
    )

    row = last_alert(conn, "item-1")
    assert row is not None
    assert row["notifier"] == "pushover"
    assert row["price_cents"] == 9000


def test_last_alert_dedup_sabotage_filtering_by_notifier_breaks_it(tmp_path, monkeypatch):
    # Sabotage check for the test above (design.md's dated entry): if
    # last_alert()'s query gained a hardcoded `AND notifier = 'discord'`
    # (the shape a naive "just check Discord's history" edit would take,
    # since Discord was the only notifier before V0.9a), a row written by
    # ANY other notifier would become invisible to dedup. This directly
    # exercises that broken query shape (built here, not by mutating
    # production code) against the same 'pushover'-tagged row the test
    # above uses, to prove that test would have caught it.
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1000)
    record_alert(
        conn, "item-1", PROFILE_ID, 2000,
        **_alert_kwargs(price_cents=9000, notifier="pushover"),
    )

    def sabotaged_last_alert(conn, item_id):
        return conn.execute(
            "SELECT * FROM alerts WHERE item_id = ? AND notifier = 'discord' "
            "ORDER BY sent_at DESC, id DESC LIMIT 1",
            (item_id,),
        ).fetchone()

    # A dedup check with the sabotaged query finds nothing, even though
    # the item was just alerted on via Pushover two seconds ago - exactly
    # the duplicate-alert bug the real last_alert() must not have.
    assert sabotaged_last_alert(conn, "item-1") is None


def test_concurrent_first_connect_against_a_fresh_file_does_not_crash_or_hang(tmp_path):
    # V0.8a regression: migration 3 (ALTER TABLE ADD COLUMN, no IF NOT
    # EXISTS equivalent in SQLite) exposed a latent race in
    # _apply_migrations - many connections opening the SAME brand-new file
    # at once could all decide to apply the same migration, and the loser
    # crashed with "duplicate column name". That's not hypothetical:
    # DailyBudget (providers/ratelimit.py) opens a fresh connection per
    # call by design and is used from many threads at once
    # (tests/test_ratelimit.py's concurrent-reservation tests hung on
    # exactly this before _apply_migrations was made to run the whole
    # read-version/apply/write-version sequence as one BEGIN
    # IMMEDIATE...COMMIT transaction). This test targets the migration
    # system directly rather than relying on DailyBudget's tests to catch
    # a regression here incidentally.
    #
    # join(timeout=...) rather than a bare join() is deliberate: a
    # reintroduced deadlock must fail this test, not hang the whole suite.
    import threading

    db_path = tmp_path / "dealwatch.db"
    errors = []
    barrier = threading.Barrier(20)

    def worker():
        barrier.wait()
        try:
            connect(db_path).close()
        except Exception as exc:  # noqa: BLE001 - captured for the assertion, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert not any(t.is_alive() for t in threads), "a thread is still stuck - deadlock reintroduced"
    assert errors == []
