"""Bounded list/histogram queries backing the V0.11 dashboard's panels
(design.md §13) - alerts-per-day, recent alerts, recent listings, and
baseline coverage. `collect_status()` (reporting/status.py) answers "how is
DealWatch doing"; this module answers "show me the last N of something,"
which is a different shape of query with different bounding rules.

Same hard contract as status.py, restated here rather than imported since
there's nothing to share but the rule itself:
  - Pure read. No INSERT/UPDATE/DELETE/BEGIN/PRAGMA, no connect() of its
    own - conn is always supplied by the caller.
  - No printing, no logging, no FastAPI import, no Settings import.
  - Never assumes conn.row_factory is set. Every query result is read by
    position, never by column name.
  - Every timestamp is a Unix second (int), never a datetime or a string.

Every query here is bounded by an indexed predicate or a LIMIT over an
index - see each function's own comment for which index. This module reads
one profile's worth of `alerts`/`listings` on every dashboard render (design
md §13's "Bound every query" decision); an unbounded scan here is exactly
the cost that turns "fine on a laptop" into "fine until the LXC has been
running a year."

`computed_baselines()` (V0.11a Part D) is the one exception that's cheap
anyway - a point read against `baselines`' own primary key. `baseline_queue()`
(V0.11a Part E) is the one exception that ISN'T bounded: it calls
engine.baselines.derive_candidates_with_stats() (V0.13; plain
derive_candidates() before that), which scans every dead spec_status=
'ok' listing and does one point query per candidate against `observations` -
orders of magnitude more work than anything else on this page, and it grows
with total history, not with the active set. Measured (design.md §13's
V0.11a addendum) at low single-digit milliseconds against a database sized
to today's real listing count, staying comfortably under 50ms even at 10x
that scale - cheap enough today to sit in the same 30s cache as everything
else, but the one function in this module that could stop being cheap
without a code change, purely from history accumulating. V0.12 Part B
adds one bounded point lookup against `listings`' own PRIMARY KEY per
rendered queue entry (at most `limit`, default 10) - negligible next to
derive_candidates() itself.

`best_ratio_window()` (V0.12 Part C4) is bounded the same way
`alerts_per_day()` is - one indexed range scan via
idx_alerts_profile_sent_at, computing only the window's two boundary
timestamps rather than one row per day.

`best_ratio_per_day()` (V0.12b Part A) is the per-day companion to
`best_ratio_window()` - one MIN(ratio_to_p25) range scan per day, same
index, same day-by-day walk `alerts_per_day()` uses (`_walk_back_days()`,
extracted so the DST-safe boundary walk exists in exactly one place).

`sweeps_per_day()` (2026-09-23, docs/learnings.md L20/L22) is the one
function in this module that does NOT hold to the "bounded by an indexed
predicate" rule above - `sweeps` carries no index at all as of schema v8,
and adding one is a schema change out of scope for the task that added
this function. Deliberate exception, not an oversight: the table grows at
a small, fixed rate from one profile's collector (~24-30 rows/day), not
with total history or the active set, so 14 unindexed range scans per
render stay cheap for a long time - see the function's own docstring.
"""

import sqlite3
import time
from datetime import datetime

from dealwatch.engine.baselines import (
    derive_candidates_with_stats,
    group_fast_candidates_by_bucket,
    select_price,
)
from dealwatch.engine.scoring import CompiledSeedBaseline, parse_spec_json, resolve_seed_baseline
from dealwatch.providers.ratelimit import PACIFIC, la_day_bounds
from dealwatch.reporting.status import event_count

# `alerts.notifiers` is list[Literal["discord", "pushover"]] - at most 2
# notifiers exist today (normalize/schema.py). recent_alerts() uses this to
# size its candidate-row fetch so a full 2-notifier fan-out on every event
# still can't starve it of distinct events.
_MAX_NOTIFIERS = 2

# No real anchor for this beyond "a bucket_key needs every segment filled
# to ever be a baseline candidate" - engine/baselines.py's own
# no_question_mark filter (derive_candidate_pool_stats) uses the identical
# rule. Kept in sync by hand, same risk status.py's _ALL_SPEC_STATUSES
# comment already flags for a different constant.
def _is_complete_bucket_key(bucket_key) -> bool:
    return bucket_key is not None and "?" not in bucket_key


def _datetime_display(ts: int | None) -> str:
    """Short LA-local date/time string ("Sep 10 14:32"), or "unknown" for a
    missing timestamp. Same reasoning as A1's per-day chart label and A6's
    price_display: date/timezone math is exactly the kind of thing the
    template must never do (design.md §13, C4/C1's hard constraints) -
    it's the one file in this codebase with no test coverage, and this
    codebase has already been burned once by timezone arithmetic done in
    the wrong place (providers/ratelimit.py's la_day_bounds() exists for
    that exact reason).
    """
    return datetime.fromtimestamp(ts, PACIFIC).strftime("%b %-d %H:%M") if ts is not None else "unknown"


def _price_display(cents: int | None) -> str:
    """"$229.99" from integer cents, or "unknown" when there's no price to
    show at all (V0.11's A6 amendment) - cents-to-dollars arithmetic
    doesn't belong in the template, and neither does the fallback text for
    a missing value; both live here so the template only ever prints a
    ready-made string.
    """
    return f"${cents / 100:,.2f}" if cents is not None else "unknown"


def _walk_back_days(now: int, days: int):
    """Yield (day_start, day_end) LA calendar-day boundaries for `days`
    days ending with today, most recent first (V0.12b Part A1) - the
    walk alerts_per_day() and best_ratio_per_day() both need, extracted
    so there is exactly one place it's written.

    Walks via la_day_bounds(cur_start - 1), never a fixed `- 86400`: a
    fixed-seconds step drifts an hour across a DST boundary and
    mislabels/misbounds every day before it (see the DST test in
    tests/test_panels.py, which spans the 2026-11-01 fall-back
    transition).
    """
    cur_start, cur_end = la_day_bounds(now)
    for _ in range(days):
        yield cur_start, cur_end
        cur_start, cur_end = la_day_bounds(cur_start - 1)


def alerts_per_day(
    conn: sqlite3.Connection, profile_id: str, *, days: int = 14, now: int | None = None
) -> list[dict]:
    """One entry per LA calendar day, oldest first:
    {"day_start", "label", "count_live", "count_dry"}.

    Split by dry_run (V0.11's A1 amendment): the live data has a 33-event
    day sitting in this window that is almost certainly V0.9 dry-run
    calibration traffic, not 33 real notifications - collect_status()'s own
    "finding" group already splits alert_events_today_live/_dry for
    exactly this reason (a mode-merged count's tallest bar can be a dry-run
    artifact, not a real signal), and a chart that re-merges them here
    would throw that distinction away one panel later.

    `label` is the short LA-calendar-day string (e.g. "Sep 8"), computed
    here rather than in the template - date formatting is timezone math,
    and the template is the one place in this codebase that isn't covered
    by a test suite.

    Walks via _walk_back_days() (V0.12b Part A1) - shared with
    best_ratio_per_day() below, not a second, separately-written walk.

    Each day's counts use status.event_count(), the same distinct-
    (item_id, sent_at) dedup collect_status() uses for its own "today"
    fields - a bare COUNT(*) here would double every bar the day a second
    notifier is enabled, while the field directly above the chart on the
    dashboard kept counting events. dry_run is constant across every
    notifier row of one event (set once per alert-cycle evaluation, not
    per notifier), so adding it to the WHERE clause splits events by mode
    without ever splitting one event across two bars.

    Indexed by idx_alerts_profile_sent_at (profile_id, sent_at) - migration
    8. Each day is two bounded range scans against that index (one per
    mode), not a scan of the whole table.
    """
    now = now if now is not None else int(time.time())

    entries = []
    for cur_start, cur_end in _walk_back_days(now, days):
        count_live = event_count(
            conn,
            "profile_id = ? AND dry_run = 0 AND sent_at >= ? AND sent_at < ?",
            (profile_id, cur_start, cur_end),
        )
        count_dry = event_count(
            conn,
            "profile_id = ? AND dry_run = 1 AND sent_at >= ? AND sent_at < ?",
            (profile_id, cur_start, cur_end),
        )
        label = datetime.fromtimestamp(cur_start, PACIFIC).strftime("%b %-d")
        entries.append(
            {
                "day_start": cur_start,
                "label": label,
                "count_live": count_live,
                "count_dry": count_dry,
            }
        )

    entries.reverse()
    return entries


def best_ratio_per_day(
    conn: sqlite3.Connection, profile_id: str, *, days: int = 14, now: int | None = None
) -> list[dict]:
    """One entry per LA calendar day, oldest first (V0.12b Part A):
    {"day_start", "label", "ratio", "baseline_layer"}.

    A1: walks via _walk_back_days(), the same shared walk alerts_per_day()
    uses - not a second implementation of the DST-safe boundary walk.

    A2: `label` is built identically to alerts_per_day()'s own
    (`datetime.fromtimestamp(cur_start, PACIFIC).strftime("%b %-d")`) so
    the two charts' day-columns line up column-for-column when stacked.
    Computed here, in Python, never in the template.

    A3: a day with no alerts returns ratio=None, baseline_layer=None -
    never ratio=0.0. A 0.0 would mean "sold for literally $0 against its
    baseline's p25," the best deal ever found - a day with nothing to
    report must not look like that day. Same "absent is not zero"
    discipline as indicators.py's unknown state, sharper here: the wrong
    value isn't just uninformative, it's backwards (the best-looking
    number the chart can show, for the day with no data at all).

    A4: no dry_run filter - matches status.py's best_ratio_24h exactly
    (`SELECT MIN(ratio_to_p25) FROM alerts WHERE profile_id = ? AND
    sent_at >= ?`, no dry_run clause). Whether a dry-run alert should
    count as "a deal found" is a real question, not an oversight this
    function is ignoring - it takes the same side best_ratio_24h already
    takes (yes), so the two ratios that can appear on this same page
    differ only because of their windows (rolling 24h vs. LA calendar
    day), never because of a silently different filter underneath. If
    that answer ever changes, it must change for both together, not one.

    A5: `baseline_layer` is read as a bare column alongside
    MIN(ratio_to_p25) in the same single-aggregate, no-GROUP-BY query -
    relying on SQLite's documented behavior that in exactly this shape,
    every bare column in the SELECT list comes from the input row that
    produced the aggregate value (https://www.sqlite.org/lang_select.html,
    "Bare columns in an aggregate query"), not an arbitrary row that
    happens to share the minimum. This is SQLite-specific and non-obvious
    enough that a reader unaware of it would reasonably assume
    `baseline_layer` is uncorrelated with the printed ratio - it isn't.
    Verified empirically against this exact query shape (a fixture where
    the minimum-ratio row's layer differs from the other rows in the same
    day) before relying on it, not just from the documentation. Chosen
    over a correlated subquery to match the one-query-per-day shape
    status.py's best_ratio_24h and this module's own best_ratio_window()
    already use - a second subquery per day would be a different query
    shape for an already-established pattern, not a simplification.

    Indexed by idx_alerts_profile_sent_at (profile_id, sent_at) -
    EXPLAIN QUERY PLAN confirms `SEARCH alerts USING INDEX
    idx_alerts_profile_sent_at (profile_id=? AND sent_at>? AND
    sent_at<?)`, the same index alerts_per_day() and best_ratio_window()
    rely on. Fourteen bounded range seeks, not fourteen scans.
    """
    now = now if now is not None else int(time.time())

    entries = []
    for cur_start, cur_end in _walk_back_days(now, days):
        ratio, baseline_layer = conn.execute(
            "SELECT MIN(ratio_to_p25), baseline_layer FROM alerts "
            "WHERE profile_id = ? AND sent_at >= ? AND sent_at < ?",
            (profile_id, cur_start, cur_end),
        ).fetchone()
        label = datetime.fromtimestamp(cur_start, PACIFIC).strftime("%b %-d")
        entries.append(
            {
                "day_start": cur_start,
                "label": label,
                "ratio": ratio,
                "baseline_layer": baseline_layer,
            }
        )

    entries.reverse()
    return entries


def sweeps_per_day(
    conn: sqlite3.Connection, profile_id: str, *, days: int = 14, now: int | None = None
) -> list[dict]:
    """One entry per LA calendar day, oldest first: {"day_start", "label",
    "sweeps", "coverage_pct"}.

    Exists for the count, not the coverage (docs/learnings.md L20, L22):
    on 2026-09-14 a nine-hour power outage produced 15 sweeps instead of
    the usual ~24, corrupting the recorded lifespan of everything that
    died during the gap, and nothing surfaced it for eight days - nothing
    in the system was watching sweep FREQUENCY, only per-sweep coverage.
    This panel is that missing signal, made visible the next morning
    instead of during the next archaeology session.

    Walks via _walk_back_days(), same as alerts_per_day()/
    best_ratio_per_day() above - a day with ZERO sweeps must appear as
    `sweeps: 0`, not be silently absent, because a missing day would hide
    exactly the outage this panel exists to catch. A GROUP BY query would
    drop that day entirely; walking the calendar and querying each day's
    bounds explicitly is what keeps it present.

    `sweeps` counts every row in the window regardless of `truncated`;
    `coverage_pct` averages `100.0 * distinct_count / active_count_before`
    only over rows where `truncated = 0`. A truncated sweep (budget
    exhausted mid-fetch) can carry `fetched_count = distinct_count = 0`,
    which is a budget event, not a coverage collapse - averaging it in
    would drag that day's mean toward zero and misrepresent a completely
    different failure as this one. If a day has sweeps but every one of
    them is truncated, `coverage_pct` is `None` for that day (as it also
    is, for the same reason, on a zero-sweep day) - never a fabricated
    0.0%, matching best_ratio_per_day()'s A3 "absent is not zero"
    discipline above. `NULLIF(active_count_before, 0)` guards the
    division itself; `active_count_before` is NOT NULL in the schema but
    has no non-zero guarantee, and AVG() over an all-NULL numerator
    correctly yields NULL rather than raising.

    Scoped to `profile_id`, like every other query in this module
    (design.md §16 P6) - `sweeps` already carries the column.

    Unlike every other per-day panel in this module, this is NOT an
    indexed range scan - `sweeps` has no index as of schema v8, and adding
    one is a schema change out of scope for the task that added this
    function. Acceptable today: the table grows at a fixed, small rate
    (~24-30 rows/day from one profile's collector, docs/learnings.md L22),
    not with the active listing set the way `listings`/`alerts` do, so 14
    small full-table scans per render stay cheap for a long time. Worth
    revisiting with a leading-`profile_id` index whenever this table is
    next touched for a schema-changing reason.
    """
    now = now if now is not None else int(time.time())

    entries = []
    for cur_start, cur_end in _walk_back_days(now, days):
        sweeps_count = conn.execute(
            "SELECT COUNT(*) FROM sweeps WHERE profile_id = ? AND swept_at >= ? AND swept_at < ?",
            (profile_id, cur_start, cur_end),
        ).fetchone()[0]
        coverage_pct = conn.execute(
            "SELECT AVG(100.0 * distinct_count / NULLIF(active_count_before, 0)) "
            "FROM sweeps WHERE profile_id = ? AND swept_at >= ? AND swept_at < ? "
            "AND truncated = 0",
            (profile_id, cur_start, cur_end),
        ).fetchone()[0]
        label = datetime.fromtimestamp(cur_start, PACIFIC).strftime("%b %-d")
        entries.append(
            {
                "day_start": cur_start,
                "label": label,
                "sweeps": sweeps_count,
                "coverage_pct": coverage_pct,
            }
        )

    entries.reverse()
    return entries


def best_ratio_window(
    conn: sqlite3.Connection, profile_id: str, *, days: int = 14, now: int | None = None
) -> float | None:
    """MIN(ratio_to_p25) over the LA-calendar-day-bounded `days`-day
    window (V0.12 Part C4) - the window `alerts_per_day(days=days)` walks
    day by day, but this only needs its two boundary timestamps, not one
    row per day. Walks back via la_day_bounds(), the same DST-safe
    boundary function alerts_per_day() uses (and for the identical
    reason: a fixed `days * 86400` window drifts an hour across a DST
    transition and silently shifts which alerts fall inside it) - not a
    reimplementation, the same function.

    Distinct from status.py's best_ratio_24h, which stays exactly as it
    is: a ROLLING 24h window, unrelated to calendar-day boundaries. Both
    may render on the same page and must be labeled with their own
    windows - the live data already shows why conflating them is wrong:
    best_ratio_24h can read 0.86 while today's LA-calendar-day alert
    count is 0, because that alert fired yesterday evening, inside the
    rolling 24h window but outside today's calendar day.

    No dry_run filter, matching best_ratio_24h's own definition
    (reporting/status.py) - neither has ever filtered by mode. Returns
    None when the window has no alerts at all, never 0.0 - a 0.0 would
    read as "found a perfect deal," not "nothing happened."

    Indexed by idx_alerts_profile_sent_at (profile_id, sent_at) - one
    bounded range scan, the same index alerts_per_day() and recent_alerts()
    already rely on.
    """
    now = now if now is not None else int(time.time())
    window_start, _ = la_day_bounds(now)
    for _ in range(days - 1):
        window_start, _ = la_day_bounds(window_start - 1)
    _, window_end = la_day_bounds(now)

    return conn.execute(
        "SELECT MIN(ratio_to_p25) FROM alerts WHERE profile_id = ? AND sent_at >= ? AND sent_at < ?",
        (profile_id, window_start, window_end),
    ).fetchone()[0]


def recent_alerts(conn: sqlite3.Connection, profile_id: str, *, limit: int = 20) -> list[dict]:
    """Most recent `limit` alert EVENTS (distinct item_id/sent_at pairs),
    most recent first - not `limit` alerts ROWS, so a fan-out to two
    notifiers does not consume two of the caller's slots.

    Two-query shape, both index-bounded:
      1. Candidate rows via idx_alerts_profile_sent_at (profile_id,
         sent_at), LIMIT `limit * _MAX_NOTIFIERS` - enough rows to
         guarantee `limit` distinct events even if every one of them
         fanned out to every configured notifier, without scanning the
         whole table.
      2. Per chosen event, the full notifier/delivery-status row set via
         idx_alerts_item_sent_at (item_id, sent_at DESC, id DESC) - bounded
         per item_id, and fetched fresh (not sliced from step 1's LIMIT)
         so a boundary event that straddled step 1's cutoff still gets
         every one of its notifier rows, not just whichever fell inside
         the first LIMIT.
    title/item_web_url come from a direct listings.item_id lookup - that
    column is the table's PRIMARY KEY, so this is a point lookup, not a
    scan.
    """
    candidate_rows = conn.execute(
        "SELECT item_id, sent_at FROM alerts WHERE profile_id = ? "
        "ORDER BY sent_at DESC, id DESC LIMIT ?",
        (profile_id, limit * _MAX_NOTIFIERS),
    ).fetchall()

    events: list[tuple] = []
    seen = set()
    for row in candidate_rows:
        item_id, sent_at = row[0], row[1]
        key = (item_id, sent_at)
        if key in seen:
            continue
        seen.add(key)
        events.append(key)
        if len(events) >= limit:
            break

    results = []
    for item_id, sent_at in events:
        notifier_rows = conn.execute(
            "SELECT notifier, delivery_status, price_cents, ratio_to_p25, "
            "baseline_layer, dry_run FROM alerts WHERE item_id = ? AND sent_at = ?",
            (item_id, sent_at),
        ).fetchall()
        delivery_statuses = {r[0]: r[1] for r in notifier_rows}
        # Every row for one (item_id, sent_at) event shares the same
        # price/ratio/layer/dry_run - they differ only in notifier and
        # delivery_status (record_alert() writes one row per notifier for
        # the same evaluated alert). Any row answers the shared fields.
        first = notifier_rows[0]

        listing = conn.execute(
            "SELECT title, item_web_url FROM listings WHERE item_id = ?",
            (item_id,),
        ).fetchone()
        title = listing[0] if listing is not None else None
        item_web_url = listing[1] if listing is not None else None

        results.append(
            {
                "item_id": item_id,
                "sent_at": sent_at,
                "sent_at_display": _datetime_display(sent_at),
                "title": title,
                "price_cents": first[2],
                "price_display": _price_display(first[2]),
                "item_web_url": item_web_url,
                "ratio_to_p25": first[3],
                "ratio_display": f"{first[3]:.2f}",
                "baseline_layer": first[4],
                "dry_run": bool(first[5]),
                "delivery_statuses": delivery_statuses,
            }
        )

    return results


def recent_listings(conn: sqlite3.Connection, profile_id: str, *, limit: int = 20) -> list[dict]:
    """Most recently first-seen `limit` listings for profile_id.

    Ordered by first_seen DESC via idx_listings_profile_first_seen
    (profile_id, first_seen) - migration 8 - with LIMIT applied over that
    index, not a full scan sorted afterward.

    `price_cents` comes from a correlated subquery against
    idx_observations_item_observed_at (item_id, observed_at) - the same
    index get_latest_observation()/get_observations() (storage/sqlite.py)
    already rely on - bounded per item_id, run once per row in the LIMITed
    outer result (at most `limit` seeks), not once per row in the whole
    table. select_price() (engine/baselines.py) picks total_cents over
    price_cents exactly the way baselines/scoring already do, rather than
    this module inventing a second "which price wins" rule.
    """
    rows = conn.execute(
        """
        SELECT
            l.item_id, l.first_seen, l.title, l.item_web_url, l.spec_status,
            l.bucket_key, l.gone_at,
            (SELECT o.total_cents FROM observations o
             WHERE o.item_id = l.item_id ORDER BY o.observed_at DESC LIMIT 1) AS total_cents,
            (SELECT o.price_cents FROM observations o
             WHERE o.item_id = l.item_id ORDER BY o.observed_at DESC LIMIT 1) AS price_cents
        FROM listings l
        WHERE l.profile_id = ?
        ORDER BY l.first_seen DESC
        LIMIT ?
        """,
        (profile_id, limit),
    ).fetchall()

    results = []
    for row in rows:
        (
            item_id, first_seen, title, item_web_url, spec_status, bucket_key,
            gone_at, total_cents, price_cents,
        ) = row
        selected = select_price(total_cents, price_cents)
        selected_price_cents = selected[0] if selected is not None else None
        results.append(
            {
                "item_id": item_id,
                "first_seen": first_seen,
                "first_seen_display": _datetime_display(first_seen),
                "title": title,
                "item_web_url": item_web_url,
                "spec_status": spec_status,
                "bucket_key": bucket_key,
                "price_cents": selected_price_cents,
                "price_display": _price_display(selected_price_cents),
                "active": gone_at is None,
            }
        )

    return results


def baseline_coverage(conn: sqlite3.Connection, profile_id: str, *, now: int | None = None) -> dict:
    """{"buckets_with_baseline", "buckets_observed", "coverage_fraction"} -
    the "buckets-at-threshold over total" panel design.md §13's rough panel
    set asks for.

    `coverage_fraction`, not `coverage_pct` (V0.11's A2 amendment): the
    value is a 0..1 fraction (0.0714 at today's 2/28), and a `_pct` name on
    a fraction is exactly the kind of mismatch indicators.py's own
    `last_sweep_coverage_pct` scaling exists to catch elsewhere - except
    this number goes straight to a template panel with no indicators.py
    layer in between to catch a scaling mistake. Renamed, not rescaled.
    `coverage_display` ("7%", or "unknown" when there's no denominator) is
    the percent-scaled, ready-to-print string - the template prints it
    as-is rather than multiplying by 100 itself, same reasoning as
    price_display (A6): arithmetic belongs in tested Python, not in the
    one file this codebase's test suite doesn't reach.

    collect_status()'s baseline group already reports
    `baseline_buckets_total` (the numerator: buckets that made it into the
    `baselines` table) and deliberately has no denominator - its own
    docstring notes that "buckets at min_samples" would always equal
    `baseline_buckets_total`, since compute_baselines() already drops
    anything under min_samples before a row is ever written. That's the
    right call for "is the baseline maturing," which only cares about
    buckets that already have one. The dashboard's coverage panel wants a
    maturity *fraction*, which needs a real denominator: how many distinct,
    complete bucket_keys exist for this profile at all, whether or not
    they've reached min_samples yet. That number isn't in status.py's
    payload, so it's computed here instead of adding a second definition
    of `baseline_buckets_total` itself.

    `buckets_with_baseline` is re-queried here rather than threaded through
    from a status payload - it's a single indexed COUNT against
    `baselines`' own primary key (profile_id, bucket_key), cheap enough
    that duplicating the read is simpler than coupling this function's
    signature to collect_status()'s output shape.

    `buckets_observed` reuses migration 8's `idx_listings_profile_first_seen`
    (profile_id, first_seen) - confirmed via `EXPLAIN QUERY PLAN`
    (`SEARCH listings USING INDEX idx_listings_profile_first_seen
    (profile_id=?)`), even though this query doesn't touch first_seen at
    all: SQLite is willing to use a composite index's leading column alone
    as an equality seek. That index exists for recent_listings()'s ORDER
    BY, not for this query - a happy side effect, not something this
    function should be relied on to keep working if that index's shape
    ever changes for recent_listings()'s sake. The `?` de-duplication
    itself still costs a temp b-tree over the matching rows (`USE TEMP
    B-TREE FOR DISTINCT`), but that set is bounded by this profile's
    active-listing count, not the whole table. Scoped to active listings
    only (gone_at IS NULL): a bucket only a long-dead listing ever
    occupied doesn't need a baseline today.
    """
    buckets_with_baseline = conn.execute(
        "SELECT COUNT(*) FROM baselines WHERE profile_id = ?", (profile_id,)
    ).fetchone()[0]

    bucket_keys = conn.execute(
        "SELECT DISTINCT bucket_key FROM listings "
        "WHERE profile_id = ? AND gone_at IS NULL AND bucket_key IS NOT NULL",
        (profile_id,),
    ).fetchall()
    buckets_observed = sum(1 for (bucket_key,) in bucket_keys if _is_complete_bucket_key(bucket_key))

    coverage_fraction = (
        buckets_with_baseline / buckets_observed if buckets_observed else None
    )
    coverage_display = (
        f"{coverage_fraction * 100:.0f}%" if coverage_fraction is not None else "unknown"
    )

    return {
        "buckets_with_baseline": buckets_with_baseline,
        "buckets_observed": buckets_observed,
        "coverage_fraction": coverage_fraction,
        "coverage_display": coverage_display,
    }


def computed_baselines(conn: sqlite3.Connection, profile_id: str) -> list[dict]:
    """One entry per row in `baselines` for this profile, ordered by
    bucket_key (V0.11a Part D).

    A point read against `baselines`' own primary key (profile_id,
    bucket_key) - returns 2 rows today, free. This is the most decision-
    relevant content on the dashboard: it's what alerts are actually
    scored against, and baseline_coverage() (above) only ever showed the
    *count* of these rows, never the numbers themselves.
    """
    rows = conn.execute(
        "SELECT bucket_key, n, n_price_only, p10_cents, p25_cents, p50_cents, "
        "fast_hours, computed_at FROM baselines WHERE profile_id = ? "
        "ORDER BY bucket_key",
        (profile_id,),
    ).fetchall()

    results = []
    for row in rows:
        (
            bucket_key, n, n_price_only, p10_cents, p25_cents, p50_cents,
            fast_hours, computed_at,
        ) = row
        results.append(
            {
                "bucket_key": bucket_key,
                "n": n,
                "n_price_only": n_price_only,
                "fast_hours": fast_hours,
                "computed_at": computed_at,
                "computed_at_display": _datetime_display(computed_at),
                "p10_display": _price_display(p10_cents),
                "p25_display": _price_display(p25_cents),
                "p50_display": _price_display(p50_cents),
            }
        )
    return results


def baseline_queue(
    conn: sqlite3.Connection,
    profile_id: str,
    *,
    min_samples: int,
    fast_lifespan_hours: int,
    compiled_seeds: list[CompiledSeedBaseline],
    limit: int = 10,
) -> dict:
    """{"queue": [...], "negative_lifespan_dropped": int} (V0.13 changed
    the return shape from a bare list to this dict - see Part B below).
    Each queue entry is a bucket that does NOT have a computed baseline
    yet, ranked by FAST-candidate count descending: {"bucket_key",
    "fast_candidates", "min_samples", "progress_pct", "seed_p25_display",
    "seed_p50_display"} (V0.11a Part E; ranking fixed in V0.11b Part A;
    progress bar and seed values added in V0.12 Parts A/B - see below).

    V0.13 Part B: `negative_lifespan_dropped` is
    derive_candidates_with_stats()'s own CandidatePoolStats field, read
    off this SAME derive call rather than a second one - the candidate
    pool this function already computes for ranking is the identical pool
    the count is about, so there is no reason to call into
    engine.baselines twice per render. Previously this number only ever
    reached a maintainer as a log line (engine/baselines.py's own INFO
    aggregate, demoted to DEBUG as of this milestone) - it is a fact
    about the data (docs/learnings.md L3), not an operational event, and
    belongs next to the queue it affects. dashboard.html renders one line
    under the queue when this is nonzero, nothing when it's zero.

    V0.12 Part A: `progress_pct` is `fast_candidates / min_samples`
    clamped to 100 for the bar's CSS width - but `fast_candidates` itself
    is NEVER clamped. recompute_baselines.py is manual (no cron, no
    collector hook - CLAUDE.md), so a bucket can sit at, say, 13/12 for
    days if nobody has run it: that's exactly the "recompute is overdue"
    signal design.md §13's V0.11a addendum surfaced as a real 6.7-day
    gap. Clamping the count itself to min_samples would render 13/12 as
    an indistinguishable, comfortably-full 12/12 and hide the exact thing
    this panel exists to catch.

    V0.12 Part B: each entry also carries the seed_baselines value this
    bucket would currently score against, resolved through
    engine.scoring.resolve_seed_baseline() - the SAME function
    score_listing() calls on every scored listing, never a second,
    hand-rolled match here. `compiled_seeds` is `compile_seed_baselines()`'s
    output (already dollars->cents converted) - this function receives it
    pre-compiled, the same way it receives min_samples/fast_lifespan_hours
    pre-resolved from the profile, rather than importing normalize.schema
    or calling compile_seed_baselines() itself.

    The spec dict `resolve_seed_baseline()` needs comes from one
    representative candidate's OWN `listings.spec_json` (parsed via
    engine.scoring.parse_spec_json() - the same helper
    scripts/score_active.py uses to build the identical dict for
    score_listing()) - never by parsing bucket_key's three pipe-delimited
    segments back into field names. bucket_key is derived FROM the spec
    by the normalize engine; reversing that derivation here would be a
    second, undocumented mapping from string position to field name that
    the normalize engine's own profile YAML already owns, and it would
    silently drift the day a profile's bucket_key shape changes. The spec
    lookup happens only for the (at most `limit`) entries that survive
    ranking, not for every distinct fast bucket - the expensive part of
    this function is already derive_candidates() below, not one more
    point lookup per rendered row.

    The representative candidate is chosen deterministically (min by
    item_id), not "whichever one derive_candidates() happens to return
    first" - _DEAD_OK_LISTINGS (engine/baselines.py) has no ORDER BY, so
    that order is an implementation artifact (SQLite's default scan
    order), not a contract, and a refresh could otherwise show a
    different seed for a bucket that hasn't actually changed. Determinism
    alone isn't what makes this sound, though - see the comment at the
    call site for the actual invariant (every seed_baselines match key in
    use today is also a bucket_key component) and what breaks it.

    seed_baselines has no RAM dimension (design.md §5.6) - a `match`
    block matches on generation/cpu_family only, so two bucket_keys
    differing only in ram_tier resolve to the IDENTICAL seed. That's
    correct, not a bug: dashboard.html carries a one-line note under this
    panel saying so, rather than leaving three visually-identical seed
    values looking like a rendering mistake.

    resolve_seed_baseline() returning None (Part B4) means the profile has
    no `match: {}` fallback entry - score_listing() treats this as a
    ValueError (a profile-authoring gap, not a listing problem); this
    panel has no listing to raise about, so it renders an explicit
    "unresolved" string for both seed fields instead of leaving them
    blank or coercing to $0.00 - the same "absent is not zero" discipline
    indicators.py's unknown state already applies to health indicators.

    Ranked by fast count, not total dead count. Qualification for a
    baseline depends entirely on the FAST population reaching
    min_samples (compute_baselines()'s own rule) - total dead candidates
    play no part in it. Ranking on the total count can point at the
    wrong bucket entirely: a bucket with 21 dead listings but only 6 fast
    ones would outrank one with 15 dead but 11 fast, even though the
    second is one candidate away from a computed baseline and the first
    isn't close. Uses group_fast_candidates_by_bucket()
    (engine/baselines.py) - the exact function compute_baselines() itself
    calls - rather than a second, hand-rolled fast-lifespan comparison,
    for the identical "one exclusion definition" reasoning E1 below
    already applies to derive_candidates() itself.

    E1 (load-bearing): reuses engine.baselines.derive_candidates_with_stats()
    (V0.13; previously plain derive_candidates()) rather than a hand-rolled
    COUNT(*). derive_candidates(), derive_candidate_pool_stats(), and
    derive_candidates_with_stats() all share one _derive() implementation
    on purpose - that module's own docstring is explicit that the
    exclusion order (sweep-confirmed, has a bucket_key, no '?', has a
    usable price, no negative lifespan) must never drift between "how many
    candidates exist" and "how many does this other reader count." A
    second, differently-shaped COUNT(*) here would fork that silently: the
    number would still look plausible and be wrong - exactly the failure
    mode design.md's baseline-poisoning trap already warns about for a
    different query. Switching to the `_with_stats` variant (rather than
    calling derive_candidates() and derive_candidate_pool_stats()
    separately) is the same reasoning one level up: this function needs
    both the candidates AND stats.negative_lifespan_dropped, and calling
    _derive() twice per render for the two halves of one already-computed
    pass would itself be exactly the kind of avoidable fork this rule
    exists to prevent.

    E2 (load-bearing): derive_candidates_with_stats()/_derive() read rows
    by column name (row["first_seen"], etc.) - written against the shape
    storage.sqlite.connect() always provides (row_factory = sqlite3.Row).
    connect_readonly() (the dashboard's own connection) deliberately does
    NOT set row_factory, and every other function in this module reads
    positionally. row_factory is flipped to sqlite3.Row for the exact
    duration of the derive_candidates_with_stats() call only, in a
    try/finally that restores whatever it was before - never left flipped
    globally, which would silently turn every OTHER positional read on
    this same connection, for the rest of this request, into a Row object
    nobody asked for.

    `fast_lifespan_hours`, like `min_samples`, always comes from the
    profile (`profile.scoring.get("fast_lifespan_hours", 24)`, main.py) -
    never a constant here, so this panel can't silently disagree with
    compute_baselines() and scripts/recompute_baselines.py about what
    "fast" means for this profile.

    Profile-scoped in the candidate-derivation step as of design.md §16 P6
    (docs/learnings.md L2, corrected 2026-09-20): derive_candidates_with_stats()
    now requires profile_id, passed through from this function's own
    argument - engine/baselines.py's _DEAD_OK_LISTINGS carries the filter,
    not a second one re-derived here. Before this correction neither was
    scoped, and the two agreed only because a single profile existed to
    agree about.
    """
    computed = {
        row[0]
        for row in conn.execute(
            "SELECT bucket_key FROM baselines WHERE profile_id = ?", (profile_id,)
        ).fetchall()
    }

    previous_row_factory = conn.row_factory
    conn.row_factory = sqlite3.Row
    try:
        candidates, stats = derive_candidates_with_stats(conn, profile_id=profile_id)
    finally:
        conn.row_factory = previous_row_factory

    fast_by_bucket = group_fast_candidates_by_bucket(candidates, fast_lifespan_hours)

    ranked = [
        (bucket_key, fast) for bucket_key, fast in fast_by_bucket.items() if bucket_key not in computed
    ]
    ranked.sort(key=lambda item: -len(item[1]))
    ranked = ranked[:limit]

    queue = []
    for bucket_key, fast in ranked:
        fast_candidates = len(fast)
        progress_pct = min(fast_candidates / min_samples, 1.0) * 100 if min_samples else 100.0

        # One representative candidate's own spec_json - not every
        # candidate's, and not derived from bucket_key. min() by item_id,
        # not fast[0]: _DEAD_OK_LISTINGS (engine/baselines.py) has no
        # ORDER BY, so "first in derive_candidates_with_stats()'s result"
        # is whatever order SQLite happens to scan the table in (empirically,
        # insertion order today) - an implementation artifact, not a
        # documented guarantee, and not safe to build a deterministic
        # panel on. min(item_id) is itself still an ARBITRARY choice of
        # listing - it is sound ONLY because every seed_baselines match
        # key in use today (generation, cpu_family) is already a
        # bucket_key component (CLAUDE.md's V0.8c bucket_key narrowing:
        # [generation, cpu_family, ram_tier]), so every listing sharing
        # this bucket_key necessarily has the same generation/cpu_family
        # and therefore resolves to the identical seed regardless of
        # which one is picked - only the CHOICE needs to be deterministic
        # (so a refresh can't show a different seed for a bucket that
        # hasn't changed), not the listing itself.
        #
        # This stops being sound the moment any seed_baselines match
        # block keys on a spec field OUTSIDE the bucket_key (condition,
        # screen, storage - anything not in [generation, cpu_family,
        # ram_tier]): two listings sharing this bucket_key could then
        # legitimately resolve to two different seeds, and "one
        # representative row" would silently pick whichever one, with no
        # way for the panel to say it's showing only one of several true
        # answers. See docs/learnings.md's entry on this exact assumption
        # before adding a seed match key outside the bucket_key fields.
        representative_item_id = min(c.item_id for c in fast)
        spec_row = conn.execute(
            "SELECT spec_json FROM listings WHERE item_id = ?", (representative_item_id,)
        ).fetchone()
        spec = parse_spec_json(spec_row[0] if spec_row is not None else None)
        seed = resolve_seed_baseline(compiled_seeds, spec)

        queue.append(
            {
                "bucket_key": bucket_key,
                "fast_candidates": fast_candidates,
                "min_samples": min_samples,
                "progress_pct": progress_pct,
                "seed_p25_display": _price_display(seed.p25_cents) if seed is not None else "unresolved",
                "seed_p50_display": _price_display(seed.p50_cents) if seed is not None else "unresolved",
            }
        )
    return {"queue": queue, "negative_lifespan_dropped": stats.negative_lifespan_dropped}
