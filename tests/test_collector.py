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
    assert profile.search.filters["price"] == [80, 1200]


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
