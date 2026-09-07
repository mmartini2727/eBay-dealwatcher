"""Tests for dealwatch.engine.collector.

FakeProvider stands in for EbayBrowseProvider - these tests exercise the
collector's orchestration (which storage calls happen, in what order, under
what failure) rather than HTTP, which test_ebay_browse.py already covers.
Real SQLite under tmp_path, no network.
"""

import asyncio
from pathlib import Path

import pytest

import dealwatch.engine.alerting as alerting_module
import dealwatch.engine.collector as collector_module
from dealwatch.config import Settings
from dealwatch.engine.collector import (
    FAST_POLL_PAGE_LIMIT,
    Collector,
    CollectorStats,
    load_profile,
    run_fast_poll_cycle,
    run_sweep_cycle,
)
from dealwatch.engine.scoring import compile_seed_baselines
from dealwatch.normalize.engine import ProfileCompileError
from dealwatch.normalize.schema import AlertsConfig, AlertTrigger, PollConfig, Profile, SearchConfig
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
        self.calls: list[tuple[str, int, int, str | None]] = []
        self._queue: list[tuple[list[dict] | Exception, int]] = []

    def queue_items(self, items: list[dict], *, reserve: int = 0) -> None:
        self._queue.append((items, reserve))

    def queue_error(self, exc: Exception) -> None:
        self._queue.append((exc, 0))

    async def search(self, profile, query, *, limit=50, max_pages=1, sort=None):
        self.calls.append((query, limit, max_pages, sort))
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


def test_poll_config_defaults_give_a_2000_listing_sweep_ceiling():
    poll = PollConfig()
    assert poll.sweep_page_limit == 200
    assert poll.sweep_max_pages == 10


def test_sweep_cycle_passes_profile_page_limit_and_max_pages_to_search(tmp_path):
    run(_sweep_cycle_passes_profile_page_limit_and_max_pages_to_search(tmp_path))


async def _sweep_cycle_passes_profile_page_limit_and_max_pages_to_search(tmp_path):
    # V0.7c: these used to be collector.py module constants
    # (SWEEP_PAGE_LIMIT/SWEEP_MAX_PAGES); asserting on the actual values
    # passed to search() is what would catch a regression back to a
    # hardcoded constant that ignores the profile.
    conn = connect(tmp_path / "dealwatch.db")
    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items([raw_item()], reserve=1)
    profile = Profile(
        id=PROFILE_ID,
        name="Test Profile",
        search=SearchConfig(
            queries=["Lenovo ThinkPad T14"],
            filters={},
            poll=PollConfig(sweep_page_limit=77, sweep_max_pages=3),
        ),
    )
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    assert provider.calls == [("Lenovo ThinkPad T14", 77, 3, None)]


def test_fast_poll_passes_the_profiles_sort_value_to_search(tmp_path):
    run(_fast_poll_passes_the_profiles_sort_value_to_search(tmp_path))


async def _fast_poll_passes_the_profiles_sort_value_to_search(tmp_path):
    # This proves run_fast_poll_cycle reads profile.search.poll.sort and
    # forwards it to search() - not that eBay does anything with it
    # (scripts/probe_sort.py already answered that). "endingSoonest" rather
    # than "newlyListed" - the profile's real value - so this can't pass by
    # coincidence if run_fast_poll_cycle ever hardcodes "newlyListed".
    conn = connect(tmp_path / "dealwatch.db")
    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items([raw_item()], reserve=1)
    profile = Profile(
        id=PROFILE_ID,
        name="Test Profile",
        search=SearchConfig(
            queries=["Lenovo ThinkPad T14"],
            filters={},
            poll=PollConfig(sort="endingSoonest"),
        ),
    )
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    assert provider.calls == [("Lenovo ThinkPad T14", FAST_POLL_PAGE_LIMIT, 1, "endingSoonest")]


def test_sweep_cycle_never_passes_sort_even_when_the_profile_sets_it(tmp_path):
    run(_sweep_cycle_never_passes_sort_even_when_the_profile_sets_it(tmp_path))


async def _sweep_cycle_never_passes_sort_even_when_the_profile_sets_it(tmp_path):
    # poll.sort is a fast-poll-only knob (design.md's V0.8e dated entry) -
    # setting it to a truthy value here and asserting the sweep's call still
    # carries sort=None is what actually proves run_sweep_cycle never reads
    # profile.search.poll.sort, as opposed to a profile that just happens to
    # leave sort unset.
    conn = connect(tmp_path / "dealwatch.db")
    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items([raw_item()], reserve=1)
    profile = Profile(
        id=PROFILE_ID,
        name="Test Profile",
        search=SearchConfig(
            queries=["Lenovo ThinkPad T14"],
            filters={},
            poll=PollConfig(sort="newlyListed"),
        ),
    )
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    assert provider.calls[0][3] is None


def test_sweep_coverage_gap_logs_warning(tmp_path, caplog):
    run(_sweep_coverage_gap_logs_warning(tmp_path, caplog))


async def _sweep_coverage_gap_logs_warning(tmp_path, caplog):
    conn = connect(tmp_path / "dealwatch.db")
    # Seed 20 listings the DB believes are still active, but the sweep
    # below will only return one of them - a coverage gap the pagination
    # horizon should have caught.
    for i in range(20):
        record_sighting(
            conn,
            f"phantom-{i}",
            dict(profile_id=PROFILE_ID, title="t"),
            dict(price_cents=10000, raw_json="{}"),
            1000,
        )

    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items([raw_item(item_id="phantom-0")], reserve=1)
    profile = make_profile()
    stats = CollectorStats()

    with caplog.at_level("WARNING"):
        await run_sweep_cycle(provider, profile, budget, conn, stats)

    assert any("coverage gap" in r.message for r in caplog.records)


def test_sweep_with_adequate_coverage_does_not_log_warning(tmp_path, caplog):
    run(_sweep_with_adequate_coverage_does_not_log_warning(tmp_path, caplog))


async def _sweep_with_adequate_coverage_does_not_log_warning(tmp_path, caplog):
    conn = connect(tmp_path / "dealwatch.db")
    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    # Single listing, seeded fresh by record_sighting inside this very
    # cycle - the DB's active count and the sweep's returned count end up
    # identical, so this must NOT warn.
    provider.queue_items([raw_item()], reserve=1)
    profile = make_profile()
    stats = CollectorStats()

    with caplog.at_level("WARNING"):
        await run_sweep_cycle(provider, profile, budget, conn, stats)

    assert not any("coverage gap" in r.message for r in caplog.records)


def test_load_profile_parses_the_real_profile_yaml():
    # Nothing else exercises Profile against the actual on-disk file - a
    # schema drift here would otherwise only surface at container startup.
    path = Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml"
    profile = load_profile(path)

    assert profile.id == "thinkpad-t14"
    assert profile.search.queries == ["Lenovo ThinkPad T14"]
    assert profile.search.poll.interval_minutes == 5
    assert profile.search.poll.sweep_interval_minutes == 60
    # V0.7c: raised from a 100 x 10 = 1,000 ceiling that fell below the
    # measured ~1,013-listing active set.
    assert profile.search.poll.sweep_page_limit == 200
    assert profile.search.poll.sweep_max_pages == 10
    # Raised from [80, 1200] at V0.7: the search filter is a fetch
    # threshold, not a buying ceiling - see alerts.max_price_usd for the
    # latter.
    assert profile.search.filters["price"] == [80, 2000]
    # V0.8b: seed_baselines is now a real modeled field (SeedBaselineEntry),
    # not swallowed by extra="ignore" - a schema drift here would otherwise
    # only surface when engine/scoring.py tries to read it. Checked
    # structurally, not against exact entries/values: the profile's actual
    # seed chart is real business data the maintainer retunes independently
    # of this schema test.
    assert len(profile.seed_baselines) > 0
    assert profile.seed_baselines[-1].match == {}  # the universal fallback
    # V0.8b: raised from 25 - see CLAUDE.md and design.md §5.3.
    assert profile.scoring["sanity_floor_pct"] == 35
    assert "best_offer_weight" not in profile.scoring  # deleted as dead config


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


def _latest_sweep_row(conn) -> dict:
    row = conn.execute("SELECT * FROM sweeps ORDER BY id DESC LIMIT 1").fetchone()
    assert row is not None, "no row written to sweeps at all"
    return dict(row)


def test_sweep_writes_a_sweeps_row_on_normal_completion(tmp_path):
    run(_sweep_writes_a_sweeps_row_on_normal_completion(tmp_path))


async def _sweep_writes_a_sweeps_row_on_normal_completion(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # One pre-existing active listing, so active_count_before is
    # verifiably non-zero rather than indistinguishable from a hardcoded 0.
    record_sighting(
        conn, "v1|pre|0",
        dict(profile_id=PROFILE_ID, title="pre-existing"),
        dict(price_cents=10000, raw_json="{}"),
        1_000_000,
    )

    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items(
        [raw_item(item_id="v1|1|0"), raw_item(item_id="v1|2|0")], reserve=1
    )
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    row = _latest_sweep_row(conn)
    assert row["fetched_count"] == 2
    assert row["distinct_count"] == 2
    assert row["active_count_before"] == 1  # only v1|pre|0 was active before this sweep
    assert row["truncated"] == 0
    assert row["sweep_recorded"] == 1


def test_sweep_writes_a_sweeps_row_on_early_budget_exhaustion(tmp_path):
    run(_sweep_writes_a_sweeps_row_on_early_budget_exhaustion(tmp_path))


async def _sweep_writes_a_sweeps_row_on_early_budget_exhaustion(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # One pre-existing active listing, same as the normal-completion test -
    # without this, active_count_before == 0 is indistinguishable from a
    # bug that never reads count_active_listings() on this exit path at
    # all. active_count_before is read at the top of run_sweep_cycle,
    # before the budget check, so it must be correct here too.
    record_sighting(
        conn, "v1|pre|0",
        dict(profile_id=PROFILE_ID, title="pre-existing"),
        dict(price_cents=10000, raw_json="{}"),
        1_000_000,
    )

    # ceiling = 0: status["remaining"] is 0 before run_sweep_cycle ever
    # calls search() - the early-return exit path, distinct from the
    # mid-sweep truncation path below.
    budget = DailyBudget(make_settings(tmp_path, daily_call_limit=0, daily_reserve_calls=0))
    provider = FakeProvider(budget)  # empty queue - a search() call here would KeyError
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    row = _latest_sweep_row(conn)
    assert row["fetched_count"] == 0
    assert row["distinct_count"] == 0
    assert row["active_count_before"] == 1  # only v1|pre|0 was active before this sweep
    assert row["truncated"] == 1
    assert row["sweep_recorded"] == 0
    assert provider.calls == []  # confirms this is genuinely the early path, not a fluke


def test_sweep_writes_a_sweeps_row_on_truncated_mid_sweep(tmp_path):
    run(_sweep_writes_a_sweeps_row_on_truncated_mid_sweep(tmp_path))


async def _sweep_writes_a_sweeps_row_on_truncated_mid_sweep(tmp_path):
    conn = connect(tmp_path / "dealwatch.db")
    # ceiling = 1: the one query call reserves the entire day's budget,
    # simulating "ran out of room mid-pagination" - same setup as
    # test_sweep_with_partial_page_set_does_not_call_record_sweep above.
    budget = DailyBudget(make_settings(tmp_path, daily_call_limit=1, daily_reserve_calls=0))
    provider = FakeProvider(budget)
    provider.queue_items([raw_item(item_id="v1|1|0")], reserve=1)
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    row = _latest_sweep_row(conn)
    assert row["fetched_count"] == 1  # the one page that DID come back before truncation
    assert row["distinct_count"] == 1
    assert row["active_count_before"] == 0
    assert row["truncated"] == 1
    assert row["sweep_recorded"] == 0


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
    # V0.7b: spec and price are independent - a mapping failure still has a
    # title, so it still gets normalized. make_profile()'s empty
    # bucket_require makes every normalize() call land on 'ok' (vacuously -
    # see test_collector_sighting_produces_a_normalized_row_not_pending for
    # the real profile's more informative case); the point here is just
    # that it is no longer left at 'pending'.
    assert row["spec_status"] != "pending"

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


def test_collector_sighting_produces_a_normalized_row_not_pending(tmp_path):
    run(_collector_sighting_produces_a_normalized_row_not_pending(tmp_path))


async def _collector_sighting_produces_a_normalized_row_not_pending(tmp_path):
    # V0.7b: the collector now calls normalize() inline on every sighting.
    # record_sighting's own 'pending' default (storage/sqlite.py; still
    # covered directly by test_storage_listings.py) is what a brand-new row
    # starts at before that call - it must not be what it's LEFT at once
    # the collector has actually run. Uses the real profile, not
    # make_profile()'s empty one, so the assertion says something (a
    # profile with no bucket_require would vacuously call everything 'ok').
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items([raw_item()])  # "Lenovo ThinkPad T14 Gen 1 16GB 256GB"
    profile = load_profile(Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml")
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    row = conn.execute(
        "SELECT spec_status, spec_json, bucket_key FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    # No CPU model in this title - generation extracts ("Gen 1") but
    # cpu_family doesn't, so bucket_require is unsatisfied: 'partial'.
    assert row["spec_status"] == "partial"


def test_title_change_goes_stale_and_is_renormalized_in_the_same_sighting(tmp_path):
    run(_title_change_goes_stale_and_is_renormalized_in_the_same_sighting(tmp_path))


async def _title_change_goes_stale_and_is_renormalized_in_the_same_sighting(tmp_path):
    # record_sighting's title-change -> 'stale' path (storage/sqlite.py) is
    # untouched by V0.7b. What's new is that the collector immediately
    # re-normalizes in the same call, so 'stale' never survives past the
    # sighting that produced it.
    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items(
        [raw_item(item_id="v1|1|0", title="Lenovo ThinkPad T14 Gen 1 16GB 256GB")]
    )
    provider.queue_items(
        [raw_item(item_id="v1|1|0", title="Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD")]
    )
    profile = load_profile(Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml")
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)
    first = conn.execute(
        "SELECT spec_status FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()
    assert first["spec_status"] == "partial"  # no cpu_family in the first title

    await run_fast_poll_cycle(provider, profile, conn, stats)
    second = conn.execute(
        "SELECT title, spec_status FROM listings WHERE item_id = 'v1|1|0'"
    ).fetchone()

    assert second["title"] == "Lenovo ThinkPad T14 Gen 1 i5-10310U 16GB RAM 256GB SSD"
    # Re-normalized against the NEW title (which now has a cpu_family, so
    # this can only be 'ok' if re-normalization actually ran against it -
    # a leftover 'partial' or a bare 'stale' would both be wrong).
    assert second["spec_status"] == "ok"


def test_normalize_error_on_one_item_does_not_abort_the_sweep(tmp_path, monkeypatch):
    run(_normalize_error_on_one_item_does_not_abort_the_sweep(tmp_path, monkeypatch))


async def _normalize_error_on_one_item_does_not_abort_the_sweep(tmp_path, monkeypatch):
    import dealwatch.engine.collector as collector_module

    real_normalize = collector_module.normalize

    def flaky_normalize(profile, listing_fields):
        if listing_fields.get("title") == "poison pill":
            raise RuntimeError("boom")
        return real_normalize(profile, listing_fields)

    monkeypatch.setattr(collector_module, "normalize", flaky_normalize)

    conn = connect(tmp_path / "dealwatch.db")
    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items(
        [
            raw_item(item_id="v1|1|0"),
            raw_item(item_id="v1|2|0", title="poison pill"),
            raw_item(item_id="v1|3|0"),
        ],
        reserve=1,
    )
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    # The sighting (record_sighting) still landed for the poisoned item -
    # only its normalization was skipped, not the whole item.
    assert get_latest_observation(conn, "v1|1|0") is not None
    assert get_latest_observation(conn, "v1|2|0") is not None
    # And the sweep kept going past the poisoned item to the next one.
    assert get_latest_observation(conn, "v1|3|0") is not None

    poisoned = conn.execute(
        "SELECT spec_status FROM listings WHERE item_id = 'v1|2|0'"
    ).fetchone()
    assert poisoned["spec_status"] == "pending"  # left untouched, recoverable by backfill

    assert stats.normalize_error_count == 1


# ---------------------------------------------------------------------------
# V0.9: alert-cycle wiring (design.md's dated entry). run_alert_cycle
# itself is fully covered by test_alerting.py - these tests only prove
# the collector calls it with the right arguments, at the right point in
# the cycle, and can't be taken down by it.
# ---------------------------------------------------------------------------


def test_fast_poll_calls_run_alert_cycle_with_the_collected_item_ids(tmp_path, monkeypatch):
    run(_fast_poll_calls_run_alert_cycle_with_the_collected_item_ids(tmp_path, monkeypatch))


async def _fast_poll_calls_run_alert_cycle_with_the_collected_item_ids(tmp_path, monkeypatch):
    calls = []

    async def fake_run_alert_cycle(conn, profile, compiled_seeds, item_ids, now_ts):
        calls.append(list(item_ids))

    monkeypatch.setattr(collector_module, "run_alert_cycle", fake_run_alert_cycle)

    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items([raw_item(item_id="v1|1|0"), raw_item(item_id="v1|2|0")])
    profile = make_profile()
    stats = CollectorStats()

    await run_fast_poll_cycle(provider, profile, conn, stats)

    assert calls == [["v1|1|0", "v1|2|0"]]


def test_fast_poll_alert_cycle_failure_does_not_propagate(tmp_path, monkeypatch):
    # Shallow variant: run_alert_cycle itself replaced wholesale. Proves the
    # collector's try/except around the CALL SITE works. It does NOT prove
    # anything about the real evaluate()/send_alert() call chain, which is
    # exactly where a real bug (a template KeyError, a bad embed field)
    # would actually originate - see the deep variant below for that.
    run(_fast_poll_alert_cycle_failure_does_not_propagate(tmp_path, monkeypatch))


async def _fast_poll_alert_cycle_failure_does_not_propagate(tmp_path, monkeypatch):
    async def broken_run_alert_cycle(*args, **kwargs):
        raise RuntimeError("Discord is down")

    monkeypatch.setattr(collector_module, "run_alert_cycle", broken_run_alert_cycle)

    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items([raw_item(item_id="v1|1|0")])
    profile = make_profile()
    stats = CollectorStats()

    # Must not raise - a bug in the alert cycle must never stall a poll
    # cycle or prevent a sighting write.
    await run_fast_poll_cycle(provider, profile, conn, stats)

    assert get_latest_observation(conn, "v1|1|0") is not None


def _alerting_enabled_profile(**alert_overrides) -> Profile:
    # A survivor of every evaluate() gate: generous trigger/ceiling, a
    # `{}` seed fallback priced so raw_item()'s default $349.99 scores as a
    # real deal (ratio_to_p25 < 1.0) - the point is to reach
    # discord.send_alert for real, not to stop earlier at some other gate.
    alerts_kwargs = dict(
        webhook_env="DISCORD_WEBHOOK_COLLECTOR_TEST",
        dry_run=False,
        trigger=AlertTrigger(max_ratio_to_p25=1.0),
        max_price_usd=1000.0,
        title_template="{generation}",
        fields=[],
    )
    alerts_kwargs.update(alert_overrides)
    return Profile(
        id=PROFILE_ID,
        name="Test Profile",
        search=SearchConfig(queries=["Lenovo ThinkPad T14"], filters={}, poll=PollConfig()),
        seed_baselines=[{"match": {}, "p25": 400, "p50": 500}],
        alerts=AlertsConfig(**alerts_kwargs),
    )


def test_fast_poll_survives_a_notifier_that_raises_deep_variant(tmp_path, monkeypatch):
    # The deep variant the shallow test above can't cover: run_alert_cycle
    # itself is real. Only discord.send_alert - the notifier - is broken,
    # simulating exactly the failure design.md calls out as the worst
    # available outcome in this milestone: a bug in the embed builder
    # (build_embed() runs OUTSIDE send_alert's own try/except, so a bad
    # template or a bad field really does propagate like this) taking down
    # the collector. This exercises the real evaluate() -> score_listing()
    # -> send_alert() chain end to end.
    run(_fast_poll_survives_a_notifier_that_raises_deep_variant(tmp_path, monkeypatch))


async def _fast_poll_survives_a_notifier_that_raises_deep_variant(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_COLLECTOR_TEST", "https://discord.example/webhook")

    async def broken_send_alert(*args, **kwargs):
        raise RuntimeError("embed builder bug")

    monkeypatch.setattr(alerting_module.discord, "send_alert", broken_send_alert)

    conn = connect(tmp_path / "dealwatch.db")
    provider = FakeProvider()
    provider.queue_items([raw_item(item_id="v1|1|0")])  # $349.99, ratio 349.99/400 < 1.0
    profile = _alerting_enabled_profile()
    compiled_seeds = compile_seed_baselines(profile)
    stats = CollectorStats()

    # Must not raise, and the sighting must still have landed - a bug deep
    # inside the notifier must never take the collector down with it.
    await run_fast_poll_cycle(provider, profile, conn, stats, compiled_seeds)

    assert get_latest_observation(conn, "v1|1|0") is not None


def test_sweep_calls_run_alert_cycle_after_record_sweep_with_seen_item_ids(tmp_path, monkeypatch):
    run(_sweep_calls_run_alert_cycle_after_record_sweep_with_seen_item_ids(tmp_path, monkeypatch))


async def _sweep_calls_run_alert_cycle_after_record_sweep_with_seen_item_ids(tmp_path, monkeypatch):
    order = []

    real_record_sweep = collector_module.record_sweep

    def spying_record_sweep(*args, **kwargs):
        order.append("record_sweep")
        return real_record_sweep(*args, **kwargs)

    async def spying_run_alert_cycle(conn, profile, compiled_seeds, item_ids, now_ts):
        order.append("run_alert_cycle")
        order.append(sorted(item_ids))

    monkeypatch.setattr(collector_module, "record_sweep", spying_record_sweep)
    monkeypatch.setattr(collector_module, "run_alert_cycle", spying_run_alert_cycle)

    conn = connect(tmp_path / "dealwatch.db")
    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items(
        [raw_item(item_id="v1|1|0"), raw_item(item_id="v1|2|0")], reserve=1
    )
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    assert order == ["record_sweep", "run_alert_cycle", ["v1|1|0", "v1|2|0"]]


def test_sweep_alert_cycle_failure_does_not_propagate(tmp_path, monkeypatch):
    run(_sweep_alert_cycle_failure_does_not_propagate(tmp_path, monkeypatch))


async def _sweep_alert_cycle_failure_does_not_propagate(tmp_path, monkeypatch):
    async def broken_run_alert_cycle(*args, **kwargs):
        raise RuntimeError("Discord is down")

    monkeypatch.setattr(collector_module, "run_alert_cycle", broken_run_alert_cycle)

    conn = connect(tmp_path / "dealwatch.db")
    budget = DailyBudget(make_settings(tmp_path))
    provider = FakeProvider(budget)
    provider.queue_items([raw_item(item_id="v1|1|0")], reserve=1)
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)  # must not raise

    # record_sweep's own bookkeeping still landed.
    row = conn.execute("SELECT last_seen FROM listings WHERE item_id = 'v1|1|0'").fetchone()
    assert row["last_seen"] is not None


def test_sweep_skips_the_alert_cycle_when_budget_is_exhausted_before_any_query(
    tmp_path, monkeypatch
):
    run(_sweep_skips_the_alert_cycle_when_budget_is_exhausted_before_any_query(tmp_path, monkeypatch))


async def _sweep_skips_the_alert_cycle_when_budget_is_exhausted_before_any_query(
    tmp_path, monkeypatch
):
    calls = []

    async def spying_run_alert_cycle(*args, **kwargs):
        calls.append(args)

    monkeypatch.setattr(collector_module, "run_alert_cycle", spying_run_alert_cycle)

    conn = connect(tmp_path / "dealwatch.db")
    budget = DailyBudget(make_settings(tmp_path, daily_call_limit=0, daily_reserve_calls=0))
    provider = FakeProvider(budget)
    profile = make_profile()
    stats = CollectorStats()

    await run_sweep_cycle(provider, profile, budget, conn, stats)

    # The early-budget-exhausted return happens before record_sweep, and
    # the alert cycle goes with it (design.md's dated entry) - alerting off
    # a sweep that never actually ran would be baseless.
    assert calls == []


# ---------------------------------------------------------------------------
# V0.9: Collector startup validation (design.md's dated entry) - fail fast
# on a bad profile OR a missing webhook env var, before either background
# loop ever runs.
# ---------------------------------------------------------------------------


def _alerts_profile(**overrides) -> Profile:
    alerts_kwargs = dict(
        webhook_env="DISCORD_WEBHOOK_COLLECTOR_TEST",
        dry_run=True,
        trigger=AlertTrigger(),
        max_price_usd=1000.0,
        title_template="{generation}",
        fields=["generation"],
    )
    alerts_kwargs.update(overrides)
    return Profile(
        id=PROFILE_ID,
        name="Test Profile",
        search=SearchConfig(queries=["q"], filters={}, poll=PollConfig()),
        alerts=AlertsConfig(**alerts_kwargs),
    )


def test_collector_init_raises_when_webhook_env_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_COLLECTOR_TEST", raising=False)
    settings = make_settings(tmp_path)
    profile = _alerts_profile()

    with pytest.raises(ProfileCompileError):
        Collector(settings, profile)


def test_collector_init_succeeds_when_webhook_env_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_COLLECTOR_TEST", "https://discord.example/webhook")
    settings = make_settings(tmp_path)
    profile = _alerts_profile()

    Collector(settings, profile)  # must not raise


def test_collector_init_does_not_require_a_webhook_env_without_an_alerts_block(tmp_path):
    settings = make_settings(tmp_path)
    profile = make_profile()  # no alerts block at all

    Collector(settings, profile)  # must not raise
