"""Collector loop: poll -> map -> persist (design.md §4.2, §7).

Two schedules, deliberately not interchangeable:

- **Fast poll** (`poll.interval_minutes`, 5): page one of a query, calls
  `record_sighting` only. Absence from a fast poll proves nothing - it's
  only ever the newest page, not the full active set.
- **Sweep** (`poll.sweep_interval_minutes`, 60): deep pagination over the
  full active set, calls `record_sighting` per item and then exactly one
  `record_sweep`. Only the sweep may establish absence - see design.md
  §4.2 and CLAUDE.md's V0.5 status block.

No scoring, no alerting, no normalize engine here - this milestone only
gets raw + mapped data into SQLite.
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from dealwatch.config import Settings
from dealwatch.normalize.engine import normalize
from dealwatch.normalize.listing import (
    Listing,
    ListingMappingError,
    _get,
    _to_float,
    _to_int,
    map_item_summary,
)
from dealwatch.normalize.schema import Profile
from dealwatch.providers.ebay import EbayBrowseProvider
from dealwatch.providers.ebay_auth import TokenManager
from dealwatch.providers.ratelimit import BudgetExhausted, DailyBudget
from dealwatch.storage.sqlite import (
    connect,
    default_db_path,
    record_sighting,
    record_sweep,
    store_spec,
)

logger = logging.getLogger(__name__)

# Page sizes and sweep depth are collector-level tuning knobs, not part of
# the profile schema (that's out of scope for this milestone) - placeholder
# values pending real volume data, same spirit as design.md §7's own
# budget-math estimate.
FAST_POLL_PAGE_LIMIT = 50
SWEEP_PAGE_LIMIT = 100
SWEEP_MAX_PAGES = 10


def load_profile(path: Path | str) -> Profile:
    with open(path) as f:
        data = yaml.safe_load(f)
    return Profile.model_validate(data)


@dataclass
class CollectorStats:
    """In-memory liveness counters for /health - not history, so a restart
    losing them is fine; the SQLite rows are the actual record."""

    last_poll_at: int | None = None
    last_sweep_at: int | None = None
    poll_count: int = 0
    sweep_count: int = 0
    mapping_error_count: int = 0
    normalize_error_count: int = 0
    cycle_error_count: int = 0

    def to_dict(self) -> dict:
        return {
            "last_poll_at": self.last_poll_at,
            "last_sweep_at": self.last_sweep_at,
            "poll_count": self.poll_count,
            "sweep_count": self.sweep_count,
            "mapping_error_count": self.mapping_error_count,
            "normalize_error_count": self.normalize_error_count,
            "cycle_error_count": self.cycle_error_count,
        }


def _listing_fields(listing: Listing, profile_id: str) -> dict:
    return {
        "profile_id": profile_id,
        "title": listing.title,
        "seller": listing.seller,
        "seller_feedback_pct": listing.seller_feedback_pct,
        "seller_feedback_score": listing.seller_feedback_score,
        "condition_id": listing.condition_id,
    }


def _observation_fields(listing: Listing, raw: dict) -> dict:
    return {
        "price_cents": listing.price_cents,
        "shipping_cents": listing.shipping_cents,
        # listing.total_cents is the Listing property, not reimplemented
        # here - it is None whenever shipping_cents is None. Recomputing it
        # by hand (e.g. price + (shipping or 0)) is exactly the bug this
        # field exists to prevent; see CLAUDE.md's V0.5 status block.
        "total_cents": listing.total_cents,
        "buying_options": listing.buying_options,
        "current_bid_cents": listing.current_bid_cents,
        "bid_count": listing.bid_count,
        "raw_json": json.dumps(raw),
    }


def _raw_only_listing_fields(raw: dict, title: str, profile_id: str) -> dict:
    """Best-effort listings row from a raw dict that failed to map. Reuses
    map_item_summary's own field-by-field extraction (_get/_to_int/_to_float
    are "private" but this is an import, not a fork - duplicating that
    parsing here would be a second copy to keep in sync by hand) for every
    field that ISN'T one of the three map_item_summary requires
    (item_id/title/price), since a missing price says nothing about
    whether seller/condition_id/etc. are present."""
    return {
        "profile_id": profile_id,
        "title": title,
        "seller": _get(raw, "seller", "username"),
        "seller_feedback_pct": _to_float(_get(raw, "seller", "feedbackPercentage")),
        "seller_feedback_score": _to_int(_get(raw, "seller", "feedbackScore")),
        "condition_id": _to_int(raw.get("conditionId")),
    }


def _normalize_input_fields(title: str, raw: dict) -> dict:
    """The dict shape normalize() expects, built from a raw dict that
    failed to map - so a mapping failure (V0.7a: still gets a listings row,
    still has a title) can still get a spec. condition_id/subtitle read the
    same way map_item_summary does; a missing price says nothing about
    whether the profile's reject/require/extract rules have what they need.
    subtitle is always None in practice (Browse returns none, design.md
    §5.1) but the key stays present rather than absent, matching what
    listing.model_dump() would give the success path - a rule naming
    `subtitle` should see "never matches", not a dict lookup miss.
    """
    return {
        "title": title,
        "subtitle": raw.get("subtitle"),
        "condition_id": _to_int(raw.get("conditionId")),
    }


def _normalize_and_store(
    conn,
    profile: Profile,
    item_id: str,
    listing_fields: dict,
    stats: CollectorStats,
) -> None:
    """Run the profile's normalize() over one listing's fields and persist
    the result via store_spec. Called after every record_sighting - both on
    a fresh insert and on an existing row, including one record_sighting
    just marked 'stale' on a title change, so 'stale' never survives past
    the sighting that produced it.

    Never raises: a normalize() failure (a regex edge case, a malformed
    listing_fields shape) must not abort the sweep it's part of - one bad
    listing left at its current spec_status (still 'pending' on a fresh
    insert) is recoverable by scripts/backfill_normalize.py once the shape
    is understood; a sweep that dies halfway loses the rest of that sweep's
    history. Same reasoning as V0.7a's persist-raw-before-mapping fix.
    """
    try:
        result = normalize(profile, listing_fields)
    except Exception:
        logger.warning(
            "normalize() failed for item_id=%s, profile=%s", item_id, profile.id,
            exc_info=True,
        )
        stats.normalize_error_count += 1
        return

    store_spec(conn, item_id, result)


def _process_raw_item(
    conn,
    profile: Profile,
    raw: dict,
    seen_at_dt: datetime,
    seen_at_ts: int,
    stats: CollectorStats,
) -> str | None:
    """Map one raw itemSummary and record_sighting it. Returns the item_id
    on success (including a mapping failure that still got a raw-only row
    written - see below), or None only when there's no item_id/title to key
    a row on at all. Never re-raises, so one bad item never loses the rest
    of the batch.

    On a ListingMappingError, still writes a listings row and an
    observations row carrying raw_json (design.md "persist raw before
    mapping" / CLAUDE.md's trap entry): raw_json is the only column every
    other one is a function of, and an item that ended last week can't be
    re-fetched, so dropping it here is permanent data loss, not a retryable
    one. spec_status is left at record_sighting's own 'pending' default -
    this is a mapping failure, not a normalization outcome, and 'pending'
    already means exactly "never normalized" (design.md §4.1).

    item_id and title are the only two fields with nowhere to fall back to
    (item_id is the primary key; title is NOT NULL in the schema) - if
    either is missing from raw, there is no row to write, and this returns
    None like a fully-skipped item always has. Per eBay's Browse API
    schema, itemId and title are both documented as always-present on an
    itemSummary (unlike price, which is legitimately absent under some
    price-display conditions), so this branch should be effectively
    unreachable against real traffic - it exists for a truly malformed
    response, not the known missing-price case this fix targets.
    """
    try:
        listing = map_item_summary(raw, seen_at_dt)
    except ListingMappingError as exc:
        stats.mapping_error_count += 1
        item_id = raw.get("itemId")
        title = raw.get("title")
        if not item_id or not title:
            logger.warning(
                "cannot persist unmappable item_id=%s: %s: %s (no item_id/title to key a row on)",
                item_id,
                type(exc).__name__,
                exc,
            )
            return None

        logger.warning(
            "failed to map item_id=%s: %s: %s", item_id, type(exc).__name__, exc
        )
        record_sighting(
            conn,
            item_id,
            _raw_only_listing_fields(raw, title, profile.id),
            # Money stays integer cents; null means unknown, never zero.
            # Deliberately not attempting buying_options/current_bid_cents/
            # bid_count here too - those parse cleanly regardless of the
            # missing-price bug, but reaching for them starts drifting
            # toward diagnosing/working around the mapping failure, which
            # is explicitly a separate task with real data in hand.
            {"price_cents": None, "shipping_cents": None, "total_cents": None,
             "raw_json": json.dumps(raw)},
            seen_at_ts,
        )
        # Spec and price are independent (V0.7b): a known spec with an
        # unknown price is still useful for bucket membership, it just
        # can't vote on a baseline. Normalize this row too.
        _normalize_and_store(conn, profile, item_id, _normalize_input_fields(title, raw), stats)
        return item_id

    record_sighting(
        conn,
        listing.item_id,
        _listing_fields(listing, profile.id),
        _observation_fields(listing, raw),
        seen_at_ts,
    )
    _normalize_and_store(conn, profile, listing.item_id, listing.model_dump(), stats)
    return listing.item_id


async def run_fast_poll_cycle(
    provider: EbayBrowseProvider,
    profile: Profile,
    conn,
    stats: CollectorStats,
) -> None:
    """One fast-poll cycle: page one of each query, record_sighting only.

    Never calls record_sweep - a fast poll only ever sees the newest page,
    so its absence-of-an-item tells you nothing about that item.
    """
    seen_at_dt = datetime.now(timezone.utc)
    seen_at_ts = int(seen_at_dt.timestamp())

    for query in profile.search.queries:
        try:
            raw_items = await provider.search(
                profile, query, limit=FAST_POLL_PAGE_LIMIT, max_pages=1
            )
        except BudgetExhausted:
            # Expected, not exceptional (see module docstring / design.md
            # §7) - log and let the next scheduled cycle try again rather
            # than busy-retrying or crashing the loop.
            logger.info(
                "fast poll for profile=%s query=%r skipped: budget exhausted",
                profile.id,
                query,
            )
            break

        for raw in raw_items:
            _process_raw_item(conn, profile, raw, seen_at_dt, seen_at_ts, stats)

    stats.last_poll_at = seen_at_ts
    stats.poll_count += 1


async def run_sweep_cycle(
    provider: EbayBrowseProvider,
    profile: Profile,
    budget: DailyBudget,
    conn,
    stats: CollectorStats,
) -> None:
    """One sweep cycle: deep pagination over the full active set, then
    exactly one record_sweep - unless the result might be truncated.

    EbayBrowseProvider.search() swallows BudgetExhausted internally once it
    has any results to return (V0.3), so a query that ran out of budget on
    page 3 of 10 looks identical, from the outside, to one that genuinely
    only had 2 pages of results. Modifying search() to expose that
    distinction is out of scope for this milestone, so this checks the
    budget directly: if it's exhausted right after a query's search() call,
    the result can't be trusted as complete, and record_sweep is skipped
    entirely for this cycle. Residual false positive: a sweep that
    legitimately finishes on its very last available unit of budget looks
    the same and also gets skipped - safe to defer to the next sweep, which
    is a much smaller cost than risking a truncated set marking real
    listings as missed (design.md §4.2).
    """
    seen_at_dt = datetime.now(timezone.utc)
    swept_at = int(seen_at_dt.timestamp())
    stats.last_sweep_at = swept_at
    stats.sweep_count += 1

    status = await asyncio.to_thread(budget.status)
    if status["remaining"] <= 0:
        logger.info("sweep for profile=%s skipped: no budget remaining", profile.id)
        return

    seen_item_ids: set[str] = set()
    exhausted = False

    for query in profile.search.queries:
        try:
            raw_items = await provider.search(
                profile, query, limit=SWEEP_PAGE_LIMIT, max_pages=SWEEP_MAX_PAGES
            )
        except BudgetExhausted:
            logger.info(
                "sweep for profile=%s query=%r hit budget exhaustion",
                profile.id,
                query,
            )
            exhausted = True
            break

        for raw in raw_items:
            item_id = _process_raw_item(conn, profile, raw, seen_at_dt, swept_at, stats)
            if item_id is not None:
                seen_item_ids.add(item_id)

        status = await asyncio.to_thread(budget.status)
        if status["remaining"] <= 0:
            logger.info(
                "sweep for profile=%s exhausted budget mid-sweep; treating "
                "result as possibly truncated",
                profile.id,
            )
            exhausted = True
            break

    if exhausted:
        # A truncated set is indistinguishable from listings having
        # disappeared (design.md §4.2) - log and skip rather than risk
        # manufacturing false misses. The items already seen above via
        # record_sighting still land; only the absence-bookkeeping is
        # skipped.
        return

    record_sweep(conn, seen_item_ids, profile.id, swept_at)


async def _fast_poll_loop(
    provider: EbayBrowseProvider, profile: Profile, conn, stats: CollectorStats
) -> None:
    interval_seconds = profile.search.poll.interval_minutes * 60
    while True:
        try:
            await run_fast_poll_cycle(provider, profile, conn, stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A collector that dies silently at 3am is the failure mode
            # this milestone exists to avoid.
            logger.exception("fast poll cycle failed for profile=%s", profile.id)
            stats.cycle_error_count += 1
        await asyncio.sleep(interval_seconds)


async def _sweep_loop(
    provider: EbayBrowseProvider,
    profile: Profile,
    budget: DailyBudget,
    conn,
    stats: CollectorStats,
) -> None:
    interval_seconds = profile.search.poll.sweep_interval_minutes * 60
    while True:
        try:
            await run_sweep_cycle(provider, profile, budget, conn, stats)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("sweep cycle failed for profile=%s", profile.id)
            stats.cycle_error_count += 1
        await asyncio.sleep(interval_seconds)


class Collector:
    """Owns the collector's one SQLite connection and its two background
    poll/sweep loops for a single profile.

    Startup does not attempt to reconcile downtime: if the container was
    down for six hours, no misses were observed during it, and the first
    sweep after restart simply resets miss_count on everything it sees, the
    same as any other sweep. Absence that was never observed is not
    absence (design.md §4.2).
    """

    def __init__(self, settings: Settings, profile: Profile) -> None:
        self.stats = CollectorStats()
        self._profile = profile
        # One connection, owned here, passed into every record_sighting/
        # record_sweep call - not the DailyBudget per-call pattern. See
        # CLAUDE.md's Conventions section on why the two differ.
        self._conn = connect(default_db_path(settings))
        self._budget = DailyBudget(settings)
        token_manager = TokenManager(settings)
        self._provider = EbayBrowseProvider(settings, token_manager, self._budget)
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        self._tasks = [
            asyncio.create_task(
                _fast_poll_loop(self._provider, self._profile, self._conn, self.stats)
            ),
            asyncio.create_task(
                _sweep_loop(
                    self._provider,
                    self._profile,
                    self._budget,
                    self._conn,
                    self.stats,
                )
            ),
        ]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self._provider.aclose()
        self._conn.close()
