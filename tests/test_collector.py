"""Tests for dealwatch.engine.collector.

FakeProvider stands in for EbayBrowseProvider - these tests exercise the
collector's orchestration (which storage calls happen, in what order, under
what failure) rather than HTTP, which test_ebay_browse.py already covers.
Real SQLite under tmp_path, no network.
"""

import asyncio
from pathlib import Path

from dealwatch.config import Settings
from dealwatch.engine.collector import (
    CollectorStats,
    load_profile,
    run_fast_poll_cycle,
    run_sweep_cycle,
)
from dealwatch.normalize.schema import PollConfig, Profile, SearchConfig
from dealwatch.providers.ratelimit import BudgetExhausted, DailyBudget
from dealwatch.storage.sqlite import (
    connect,
    get_latest_observation,
    get_observations,
    record_sighting,
)

PROFILE_ID = "thinkpad-t14"


class FakeProvider:
    """Queue of canned responses, one per expected search() call, in order.

    A queued item is either a list[dict] (a page of raw itemSummaries) or
    an Exception instance to raise. `reserve` lets a test simulate how much
    real budget that call would have consumed, since FakeProvider doesn't
    make real HTTP requests and so never calls budget.reserve() itself.
    """

    def __init__(self, budget: DailyBudget | None = None):
        self.budget = budget
        self.calls: list[tuple[str, int, int]] = []
        self._queue: list[tuple[list[dict] | Exception, int]] = []

    def queue_items(self, items: list[dict], *, reserve: int = 0) -> None:
        self._queue.append((items, reserve))

    def queue_error(self, exc: Exception) -> None:
        self._queue.append((exc, 0))

    async def search(self, profile, query, *, limit=50, max_pages=1):
        self.calls.append((query, limit, max_pages))
        payload, reserve_n = self._queue.pop(0)
        for _ in range(reserve_n):
            assert self.budget is not None
            self.budget.reserve()
        if isinstance(payload, Exception):
            raise payload
        return payload


def make_settings(tmp_path, **overrides):
    defaults = dict(
        db_path=str(tmp_path / "dealwatch.db"),
        daily_call_limit=1000,
        daily_reserve_calls=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_profile() -> Profile:
    return Profile(
        id=PROFILE_ID,
        name="Test Profile",
        search=SearchConfig(
            queries=["Lenovo ThinkPad T14"],
            filters={},
            poll=PollConfig(interval_minutes=5, sweep_interval_minutes=60),
        ),
    )


def raw_item(item_id="v1|1|0", title="Lenovo ThinkPad T14 Gen 1 16GB 256GB",
             price="349.99", shipping=None, **overrides) -> dict:
    item = {
        "itemId": item_id,
        "title": title,
        "price": {"value": price, "currency": "USD"},
        "buyingOptions": ["FIXED_PRICE"],
    }
    if shipping is not None:
        item["shippingOptions"] = [
            {"shippingCost": {"value": shipping, "currency": "USD"}}
        ]
    item.update(overrides)
    return item


def run(coro):
    return asyncio.run(coro)


def test_load_profile_parses_the_real_profile_yaml():
    # Nothing else exercises Profile against the actual on-disk file - a
    # schema drift here would otherwise only surface at container startup.
    path = Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml"
    profile = load_profile(path)

    assert profile.id == "thinkpad-t14"
    assert profile.search.queries == ["Lenovo ThinkPad T14"]
    assert profile.search.poll.interval_minutes == 5
    assert profile.search.poll.sweep_interval_minutes == 60
    # Raised from [80, 1200] at V0.7: the search filter is a fetch
    # threshold, not a buying ceiling - see alerts.max_price_usd for the
    # latter.
    assert profile.search.filters["price"] == [80, 2000]


def test_fast_poll_cycle_inserts_and_does_not_advance_last_seen(tmp_path):
    run(_fast_poll_cycle_inserts_and_does_not_advance_last_seen(tmp_path))


async def _fast_poll_cycle_inserts_and_does_not_advance_last_seen(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")

    # Seed the item as already existing, with an old last_seen far in the
    # past. On a brand-new row, first_seen and last_seen bootstrap to the
    # SAME value, so a single fast-poll cycle on a never-seen-before item
    # can't distinguish "last_seen was never touched" from "last_seen was
    # set to a value that happens to match" - both look identical. Seeding
    # an old, distinct last_seen and then polling with FakeProvider (whose
    # timestamps come from real datetime.now(), i.e. far later) makes an
    # accidental advance to "now" clearly visible.
    old_seen_at = 1_000_000
    record_sighting(
        conn,
        "v1|1|0",
        dict(profile_id=PROFILE_ID, title=raw_item()["title"]),
        dict(price_cents=34999, buying_options=["FIXED_PRICE"], raw_json="{}"),
        old_seen_at,
    )

    provider = FakeProvider()
    provider.queue_items([raw_item()])
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    observations = get_observations(conn, "v1|1|0")
    assert len(observations) == 1  # unchanged fields -> no second observation

    row = conn.execute(
        "SELECT last_seen FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    # Only record_sweep is allowed to advance last_seen - a fast poll must
    # leave it exactly where it was.
    assert row["last_seen"] == old_seen_at
    assert stats.poll_count == 1


def test_sweep_cycle_advances_last_seen_for_everything_returned(tmp_path):
    run(_sweep_cycle_advances_last_seen_for_everything_returned(tmp_path))


async def _sweep_cycle_advances_last_seen_for_everything_returned(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")

    # Seed with an old last_seen distinct from "now" - on a same-cycle
    # brand-new insert, first_seen/last_seen/stats.last_sweep_at would all
    # bootstrap to the identical current timestamp, making "last_seen ==
    # stats.last_sweep_at" true even if record_sweep never ran.
    old_seen_at = 1_000_000
    record_sighting(
        conn,
        "v1|1|0",
        dict(profile_id=PROFILE_ID, title=raw_item()["title"]),
        dict(price_cents=34999, buying_options=["FIXED_PRICE"], raw_json="{}"),
        old_seen_at,
    )

    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items([raw_item()], reserve=1)
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    row = conn.execute(
        "SELECT last_seen FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row["last_seen"] == stats.last_sweep_at
    assert row["last_seen"] != old_seen_at
    assert stats.sweep_count == 1


def test_sweep_with_partial_page_set_does_not_call_record_sweep(tmp_path):
    run(_sweep_with_partial_page_set_does_not_call_record_sweep(tmp_path))


async def _sweep_with_partial_page_set_does_not_call_record_sweep(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")

    # Seed an old last_seen, distinct from "now" - see the sibling test
    # above for why comparing against first_seen/stats set in the SAME
    # cycle can't actually distinguish "record_sweep ran" from "it didn't."
    old_seen_at = 1_000_000
    record_sighting(
        conn,
        "v1|1|0",
        dict(profile_id=PROFILE_ID, title=raw_item()["title"]),
        dict(price_cents=34999, buying_options=["FIXED_PRICE"], raw_json="{}"),
        old_seen_at,
    )

    # ceiling = 1: the fake's one query call reserves the entire day's
    # budget, simulating "ran out of room mid-pagination."
    budget = DailyBudget(make_settings(tmp_path, daily_call_limit=1, daily_reserve_calls=0))
    provider = FakeProvider(budget)
    provider.queue_items([raw_item()], reserve=1)
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    # The item's own sighting still landed (already-paid-for data isn't
    # thrown away)...
    assert get_latest_observation(conn, "v1|1|0") is not None
    # ...but the absence-establishing bookkeeping did not run: last_seen
    # never advanced past the old seeded value.
    row = conn.execute(
        "SELECT last_seen FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row["last_seen"] == old_seen_at


def test_mapping_failure_is_isolated_surrounding_items_still_land(tmp_path):
    run(_mapping_failure_is_isolated_surrounding_items_still_land(tmp_path))


async def _mapping_failure_is_isolated_surrounding_items_still_land(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    bad_item = {"title": "missing itemId and price entirely"}
    provider.queue_items(
        [raw_item(item_id="v1|1|0"), bad_item, raw_item(item_id="v1|2|0")]
    )
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    assert get_latest_observation(conn, "v1|1|0") is not None
    assert get_latest_observation(conn, "v1|2|0") is not None
    assert stats.mapping_error_count == 1


def test_unmappable_item_with_no_price_still_writes_both_rows(tmp_path):
    run(_unmappable_item_with_no_price_still_writes_both_rows(tmp_path))


async def _unmappable_item_with_no_price_still_writes_both_rows(tmp_path):
    # The real bug this fix exists for: ~6/sweep fixed-price listings with
    # no `price` field at all. item_id and title are both present - only
    # price is missing - so there's no reason this history should be lost.
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    bad_item = raw_item(item_id="v1|1|0")
    del bad_item["price"]
    provider.queue_items([bad_item])
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    row = conn.execute(
        "SELECT title, spec_status FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row is not None
    assert row["title"] == bad_item["title"]
    assert row["spec_status"] == "pending"  # a mapping failure, not a normalization outcome

    obs = get_latest_observation(conn, "v1|1|0")
    assert obs is not None
    assert obs["price_cents"] is None
    assert obs["raw_json"] is not None
    assert stats.mapping_error_count == 1


def test_unmappable_item_still_captures_seller_and_condition_id(tmp_path):
    run(_unmappable_item_still_captures_seller_and_condition_id(tmp_path))


async def _unmappable_item_still_captures_seller_and_condition_id(tmp_path):
    # "the fields that ARE present" - a missing price says nothing about
    # whether seller/condition_id are present, so they shouldn't be thrown
    # away along with the fields that genuinely aren't there.
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    bad_item = raw_item(
        item_id="v1|1|0",
        conditionId="3000",
        seller={"username": "gooddeals99", "feedbackPercentage": "99.1", "feedbackScore": 4200},
    )
    del bad_item["price"]
    provider.queue_items([bad_item])
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    row = conn.execute(
        "SELECT seller, seller_feedback_pct, seller_feedback_score, condition_id "
        "FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row["seller"] == "gooddeals99"
    assert row["seller_feedback_pct"] == 99.1
    assert row["seller_feedback_score"] == 4200
    assert row["condition_id"] == 3000


def test_unmappable_item_missing_item_id_or_title_is_skipped_not_written(tmp_path):
    run(_unmappable_item_missing_item_id_or_title_is_skipped_not_written(tmp_path))


async def _unmappable_item_missing_item_id_or_title_is_skipped_not_written(tmp_path):
    # No item_id (PRIMARY KEY) and no title (NOT NULL) - there's no row to
    # write. This differs from the no-price case above: the same
    # ListingMappingError fires, but here there's genuinely nothing to key
    # a row on. Per eBay's Browse API schema this should be unreachable in
    # practice (itemId/title are always present; price is the one that
    # legitimately isn't), so this is a defensive path, not a live gap.
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items([{"title": "no itemId at all", "price": {"value": "10.00"}}])
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)  # must not raise

    assert conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0] == 0
    assert stats.mapping_error_count == 1


def test_unmappable_item_repeated_across_cycles_does_not_duplicate_listings_row(tmp_path):
    run(_unmappable_item_repeated_across_cycles_does_not_duplicate_listings_row(tmp_path))


async def _unmappable_item_repeated_across_cycles_does_not_duplicate_listings_row(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    bad_item = raw_item(item_id="v1|1|0")
    del bad_item["price"]
    provider.queue_items([bad_item])
    provider.queue_items([bad_item])  # still unmappable on a later cycle
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)
    await run_fast_poll_cycle(provider, profile, conn, stats)

    count = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()[0]
    assert count == 1
    assert stats.mapping_error_count == 2


def test_unmappable_item_in_a_sweep_does_not_get_miss_count_incremented(tmp_path):
    run(_unmappable_item_in_a_sweep_does_not_get_miss_count_incremented(tmp_path))


async def _unmappable_item_in_a_sweep_does_not_get_miss_count_incremented(tmp_path):
    # The subtle failure mode the task calls out explicitly: if an
    # unmappable-but-present item isn't added to the sweep's seen set, its
    # miss_count climbs while it's sitting right there in the search
    # results, and it gets marked gone at N=3 with a fabricated lifespan.
    conn = connect(tmp_path / "dealwatch.db")
    bad_item = raw_item(item_id="v1|1|0")
    del bad_item["price"]

    old_seen_at = 1_000_000
    # Seed the listing as already existing (as if a prior sweep wrote the
    # raw-only row) so record_sweep's miss_count bookkeeping has something
    # to (not) act on.
    record_sighting(
        conn,
        "v1|1|0",
        dict(profile_id=PROFILE_ID, title=bad_item["title"]),
        dict(price_cents=None, raw_json="{}"),
        old_seen_at,
    )

    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items([bad_item], reserve=1)
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    row = conn.execute(
        "SELECT miss_count, last_seen FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row["miss_count"] == 0
    assert row["last_seen"] != old_seen_at  # still counted as seen


def test_item_that_maps_successfully_after_a_prior_failure_gets_price_populated(tmp_path):
    run(_item_that_maps_successfully_after_a_prior_failure_gets_price_populated(tmp_path))


async def _item_that_maps_successfully_after_a_prior_failure_gets_price_populated(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    bad_item = raw_item(item_id="v1|1|0")
    del bad_item["price"]
    provider.queue_items([bad_item])
    provider.queue_items([raw_item(item_id="v1|1|0")])  # same item, now mappable
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)
    await run_fast_poll_cycle(provider, profile, conn, stats)

    observations = get_observations(conn, "v1|1|0")
    assert len(observations) == 2  # None -> a real price counts as a change
    assert observations[0]["price_cents"] is None
    assert observations[-1]["price_cents"] == 34999


def test_budget_exhausted_mid_cycle_does_not_propagate(tmp_path):
    run(_budget_exhausted_mid_cycle_does_not_propagate(tmp_path))


async def _budget_exhausted_mid_cycle_does_not_propagate(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_error(BudgetExhausted({"remaining": 0}))
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)  # must not raise

    assert stats.poll_count == 1


def test_unknown_shipping_produces_null_total_not_total_equals_price(tmp_path):
    run(_unknown_shipping_produces_null_total_not_total_equals_price(tmp_path))


async def _unknown_shipping_produces_null_total_not_total_equals_price(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items([raw_item(shipping=None)])  # no shippingOptions key
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    obs = get_latest_observation(conn, "v1|1|0")
    assert obs["shipping_cents"] is None
    assert obs["total_cents"] is None


def test_two_identical_cycles_write_one_observation_per_item(tmp_path):
    run(_two_identical_cycles_write_one_observation_per_item(tmp_path))


async def _two_identical_cycles_write_one_observation_per_item(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items([raw_item()])
    provider.queue_items([raw_item()])  # identical second cycle
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)
    await run_fast_poll_cycle(provider, profile, conn, stats)

    assert len(get_observations(conn, "v1|1|0")) == 1
    assert stats.poll_count == 2


def test_every_row_written_carries_spec_status(tmp_path):
    run(_every_row_written_carries_spec_status(tmp_path))


async def _every_row_written_carries_spec_status(tmp_path):
    # record_sighting (storage/sqlite.py) now defaults spec_status to
    # 'pending' for a brand-new row when the caller doesn't specify one -
    # the collector doesn't pass spec_status at all, so every row it writes
    # gets that default. (Previously this hardcoded 'stale', which is
    # wrong for a listing that has never been normalized; fixed in the
    # V0.5 correction.)
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items([raw_item()])
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    row = conn.execute(
        "SELECT spec_status FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert row["spec_status"] == "pending"
