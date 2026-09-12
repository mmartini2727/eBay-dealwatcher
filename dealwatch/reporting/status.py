"""collect_status() - one function that answers "how is DealWatch doing
right now," from the database, for every consumer (V0.10, design.md §12).

`scripts/status.py` prints this payload; a future `/status` route
(main.py, not this milestone), the V0.11 dashboard, and V1.0's MCP
`status()` tool are all meant to call collect_status() directly rather
than re-deriving "sweeps today" a fourth time. That drift risk is the
entire reason this module exists rather than four ad-hoc query sets - the
same "one path, not several that quietly diverge" reasoning CLAUDE.md
already states for the collector/backfill_normalize.py's
normalize_input_fields() split.

Do not confuse this module with `CollectorStats` (engine/collector.py),
already exposed under /health's "collector" key. CollectorStats is
in-memory and resets on every container restart - it answers "what has
this process done since it booted." Everything collect_status() reports
is read from persisted tables and (for "today"/"7d"/"24h" fields) scoped
to real wall-clock windows, not process uptime. A CollectorStats
last_sweep_at of five minutes ago and a collect_status()
last_sweep_started_at of five minutes ago are two independent
measurements that happen to agree today; they will not always agree
(e.g. right after a restart, CollectorStats resets to None while
collect_status() still reports the last real sweep from before the
restart). Whoever wires both into one dashboard or route needs to show
them as what they are, not merge them into one number.

Hard contract, enforced by tests/test_status.py, not just described here:
  - Pure read. No INSERT/UPDATE/DELETE/BEGIN/PRAGMA, no connect() of its
    own - conn is always supplied by the caller.
  - No printing, no logging, no FastAPI import, no Settings import, no
    profile loading. Formatting and I/O are every caller's own job.
  - Never assumes conn.row_factory is set. Both of today's actual callers
    (storage.sqlite.connect() and scripts/'s open_readonly()) happen to
    set sqlite3.Row, but this is a contract for consumers that don't
    exist yet, not a live bug being worked around - every query result is
    read by position, never by column name.
  - Every timestamp in the payload is a Unix second (int) or an
    age-in-minutes (int); never a datetime object or a string. Any age
    field is None exactly when its underlying timestamp is None.

Two clocks, deliberately, and the field names carry which is which:
  - "today" = day_start <= ts < day_end, the America/Los_Angeles calendar
    day containing `now` (providers.ratelimit.la_day_bounds).
  - "7d" = ts >= now - 7*86400. Rolling, not calendar.
  - "24h" = ts >= now - 86400. Rolling, not calendar.

Payload shape - these key names are the contract three future consumers
will hard-code, so changing one is a breaking change to all of them:

  "alive": {
      last_sweep_started_at, last_sweep_started_age_mins,
          # max(sweeps.swept_at) WHERE sweep_recorded = 1 - stamped at
          # cycle START (a sweep takes minutes), not completion.
      last_sweep_attempt_at, last_sweep_attempt_age_mins,
          # max(sweeps.swept_at), any row - includes skipped/truncated
          # attempts, so this can be newer than last_sweep_started_at.
      last_price_change_at, last_price_change_age_mins,
          # Newest observations row for this profile. NOT a liveness
          # signal on its own: record_sighting() never writes last_seen,
          # and a fast poll against a quiet market (no price/shipping/
          # buying_options change) writes no observations row at all -
          # this answers "has any price moved recently," nothing more.
      sweeps_today_total, sweeps_today_recorded, sweeps_today_truncated,
      listings_last_seen_max,
          # max(listings.last_seen) for this profile.
      sweep_bookkeeping_consistent,
          # listings_last_seen_max == last_sweep_started_at - only
          # record_sweep() ever advances last_seen, so these should
          # agree. None (not False) when there is no recorded sweep yet,
          # or when the latest recorded sweep's distinct_count is 0: a
          # sweep that returned zero items advances no listing's
          # last_seen, so the invariant is unevaluable, not violated -
          # same "unevaluable is not the same as false" discipline as
          # last_sweep_coverage_pct below.
      budget: {period, used, period_is_today, ceiling, remaining},
          # Read from the `budget` table directly - NEVER via
          # DailyBudget.status(), which opens its own connection, runs
          # migrations, and WRITES the lazy rollover UPDATE. `period`/
          # `used` are reported exactly as stored; a stale period (from
          # before the day rolled over) is surfaced via
          # period_is_today=False rather than silently zeroed here.
          # `ceiling` is not in the database (daily_call_limit -
          # daily_reserve_calls, a Settings value) - the caller passes
          # it in. When ceiling is None, both ceiling and remaining are
          # None (the keys are always present either way).
  }

  "collecting": {
      active_listings,          # gone_at IS NULL
      new_listings_today,       # first_seen within today
      deaths_today,             # gone_at within today
      deaths_today_unmeasured,
          # gone_at within today AND lifespan_mins IS NULL (V0.9b) - a
          # listing that died without ever being confirmed by a sweep.
          # Deliberately NOT first_seen == last_seen: that comparison is
          # engine.baselines.derive_candidates()'s own authoritative
          # never-swept filter and stays in exactly one place. A row
          # that died before V0.9b shipped still has lifespan_mins = 0,
          # not NULL, unless scripts/backfill_zero_lifespan.py has been
          # run against it - this field is only truthful for recent
          # deaths, which is all "today" ever covers anyway.
      spec_status_counts_active,
          # dict, spec_status -> count, scoped to gone_at IS NULL only -
          # an all-time breakdown is dominated by history and never
          # moves, so it can't answer "is normalization keeping up
          # right now."
      last_sweep_coverage_pct, last_sweep_page_drift,
      last_sweep_active_count_before,
          # All three from the latest sweeps row WHERE sweep_recorded=1
          # for this profile - NOT the latest row overall, since a
          # budget-exhausted early return writes distinct_count=0 and
          # would otherwise report a false 0% coverage collapse.
          # last_sweep_coverage_pct is None (not 0.0, not a
          # ZeroDivisionError) when active_count_before is 0.
  }

  "finding": {
      alert_events_today_live, alert_events_today_dry, alert_rows_today,
      alert_events_7d, distinct_items_alerted_7d, best_ratio_24h,
      delivery_status_row_counts_today,
          # An alert EVENT is a distinct (item_id, sent_at) pair; `alerts`
          # fans out one row per configured notifier (migration 7), so a
          # bare COUNT(*) overcounts events by the channel count.
          # alert_rows_today and delivery_status_row_counts_today count
          # ROWS on purpose - one notifier succeeding while another fails
          # is exactly what the row-level counts exist to show, and their
          # values must never be summed against the event counts.
          # "_live" means dry_run=0, which INCLUDES failed/skipped
          # deliveries (notifier='none', delivery_status='skipped') - it
          # means "this would have been a real send," not "this was
          # successfully delivered."
  }

  "baseline": {
      baseline_buckets_total, baselines_computed_age_mins,
      dead_spec_ok_count, dead_spec_ok_deaths_7d,
          # dead_spec_ok_count is scoped to this profile_id, but
          # engine.baselines._DEAD_OK_LISTINGS is NOT profile-scoped -
          # the two numbers agree today (one profile exists) and will
          # diverge the day a second profile does.
      baseline_layer_counts_7d,
          # dict, alerts.baseline_layer -> EVENT count (same dedup as
          # "finding" above). An alerts-only proxy for "how often are we
          # still falling back to a seed" - biased toward the
          # ratio-triggered tail, since a scoring result is only ever
          # persisted for a listing that actually alerted. There is no
          # per-listing scored-layer column, so the unbiased version
          # (every scored listing, alerted or not) is not computable
          # from the current schema.
          #
          # Deliberately NO baseline_buckets_at_min_samples field:
          # compute_baselines() already skips any bucket under
          # min_samples, so every row in `baselines` already qualifies -
          # that field would always equal baseline_buckets_total and
          # read as "100% mature." Real convergence analysis (the
          # candidate-pool funnel, threshold sensitivity, the
          # falsification check) lives in scripts/baseline_report.py and
          # is deliberately not duplicated here.
  }
"""

import sqlite3
import time
from datetime import datetime

from dealwatch.providers.ratelimit import PACIFIC, la_day_bounds

_REJECTED = "rejected"
_NOT_TARGET = "not_target"
_OK = "ok"
_PARTIAL = "partial"
_PENDING = "pending"
_STALE = "stale"
# Mirrors dealwatch.normalize.engine's REJECTED/NOT_TARGET/OK/PARTIAL
# constants plus the two bare-literal statuses storage.sqlite.
# record_sighting() writes ("pending" on first insert, "stale" when a
# title changes) - not imported from normalize/engine.py because doing so
# would pull a normalization-pipeline import into a module whose whole
# point is having none. A 7th spec_status value added there without a
# matching update here would silently disappear from
# spec_status_counts_active - this list is the one place that risk lives,
# and it is exactly the kind of two-definitions-of-one-fact drift this
# milestone otherwise exists to prevent, so keep it in sync by hand if
# normalize/engine.py's set of statuses ever changes.
_ALL_SPEC_STATUSES = (_REJECTED, _NOT_TARGET, _OK, _PARTIAL, _PENDING, _STALE)


def _age_mins(ts: int | None, now: int) -> int | None:
    return (now - ts) // 60 if ts is not None else None


def event_count(conn: sqlite3.Connection, sql_where: str, params: tuple) -> int:
    """COUNT of distinct (item_id, sent_at) pairs matching sql_where - one
    alert EVENT, not one alerts ROW. Not COUNT(DISTINCT item_id || '|' ||
    sent_at): item_ids already contain pipes, so that concatenation is not
    actually collision-free.

    Not prefixed with `_` (V0.11, design.md §13): reporting/panels.py's
    alerts_per_day() needs this exact dedup rule to count histogram bars
    the same way collect_status()'s own alert_events_* fields do - a
    second definition here would let the dashboard's daily bars silently
    disagree with the status payload's "today" count sitting right above
    them the day a second notifier is enabled.
    """
    row = conn.execute(
        f"SELECT COUNT(*) FROM (SELECT DISTINCT item_id, sent_at FROM alerts "
        f"WHERE {sql_where})",
        params,
    ).fetchone()
    return row[0]


def _alive(
    conn: sqlite3.Connection,
    profile_id: str,
    day_start: int,
    day_end: int,
    now: int,
    ceiling: int | None,
) -> dict:
    latest_recorded = conn.execute(
        "SELECT swept_at, distinct_count FROM sweeps "
        "WHERE profile_id = ? AND sweep_recorded = 1 "
        "ORDER BY swept_at DESC, id DESC LIMIT 1",
        (profile_id,),
    ).fetchone()
    last_sweep_started_at = latest_recorded[0] if latest_recorded else None

    listings_last_seen_max = conn.execute(
        "SELECT MAX(last_seen) FROM listings WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()[0]

    if latest_recorded is None or latest_recorded[1] == 0:
        sweep_bookkeeping_consistent = None
    else:
        sweep_bookkeeping_consistent = listings_last_seen_max == last_sweep_started_at

    last_sweep_attempt_at = conn.execute(
        "SELECT MAX(swept_at) FROM sweeps WHERE profile_id = ?",
        (profile_id,),
    ).fetchone()[0]

    # Not MAX(observed_at): idx_observations_item_observed_at leads on
    # item_id, so a MAX over every item_id for this profile can't use it,
    # and observations is the largest table in this schema. id is
    # monotonic with insert order on an append-only table, so ORDER BY
    # id DESC LIMIT 1 walks observations backwards by rowid, checking each
    # row's owning listing via an indexed item_id lookup, and stops at the
    # first one whose profile_id matches - confirmed via EXPLAIN QUERY
    # PLAN (no "USE TEMP B-TREE FOR ORDER BY": no materialize-then-sort
    # step) and via instruction-level profiling (~20 VM steps against a
    # 2,500-row observations table today, i.e. genuinely O(1) against a
    # single-profile database, not a disguised full scan).
    #
    # That good case is profile-dependent, not schema-dependent: the walk
    # only stops early because every observation currently in the table
    # belongs to the one profile being queried. Measured directly (not
    # inferred) against a synthetic second profile whose 2,500
    # observations were all more recent: the same query costs ~15,000 VM
    # steps instead of ~20 - it must walk past every one of the OTHER
    # profile's newer rows before reaching this profile's own last one.
    # An EXISTS-based rewrite of this same query was measured too and
    # produces an identical plan and step count - SQLite optimizes both
    # shapes the same way, so switching form does not fix this. A real
    # fix needs either a profile_id column on observations or a separate
    # per-profile tracking structure, both schema changes this milestone
    # is scoped not to make (see module's "Out of scope"). Safe for
    # today's single-profile deployment; V0.11 should not assume this
    # query stays O(1) once a second profile exists.
    last_price_change_at = conn.execute(
        "SELECT o.observed_at FROM observations o JOIN listings l USING (item_id) "
        "WHERE l.profile_id = ? ORDER BY o.id DESC LIMIT 1",
        (profile_id,),
    ).fetchone()
    last_price_change_at = last_price_change_at[0] if last_price_change_at else None

    sweeps_today = conn.execute(
        "SELECT COUNT(*), "
        "SUM(CASE WHEN sweep_recorded = 1 THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN truncated = 1 THEN 1 ELSE 0 END) "
        "FROM sweeps WHERE profile_id = ? AND swept_at >= ? AND swept_at < ?",
        (profile_id, day_start, day_end),
    ).fetchone()
    sweeps_today_total = sweeps_today[0]
    sweeps_today_recorded = sweeps_today[1] or 0
    sweeps_today_truncated = sweeps_today[2] or 0

    budget_row = conn.execute("SELECT period, used FROM budget WHERE id = 1").fetchone()
    if budget_row is None:
        period, used, period_is_today = None, None, None
    else:
        period, used = budget_row[0], budget_row[1]
        today_date = datetime.fromtimestamp(day_start, PACIFIC).date().isoformat()
        period_is_today = period == today_date

    if ceiling is None:
        remaining = None
    else:
        remaining = max(ceiling - used, 0) if used is not None else None

    return {
        "last_sweep_started_at": last_sweep_started_at,
        "last_sweep_started_age_mins": _age_mins(last_sweep_started_at, now),
        "last_sweep_attempt_at": last_sweep_attempt_at,
        "last_sweep_attempt_age_mins": _age_mins(last_sweep_attempt_at, now),
        "last_price_change_at": last_price_change_at,
        "last_price_change_age_mins": _age_mins(last_price_change_at, now),
        "sweeps_today_total": sweeps_today_total,
        "sweeps_today_recorded": sweeps_today_recorded,
        "sweeps_today_truncated": sweeps_today_truncated,
        "listings_last_seen_max": listings_last_seen_max,
        "sweep_bookkeeping_consistent": sweep_bookkeeping_consistent,
        "budget": {
            "period": period,
            "used": used,
            "period_is_today": period_is_today,
            "ceiling": ceiling,
            "remaining": remaining,
        },
    }


def _collecting(
    conn: sqlite3.Connection, profile_id: str, day_start: int, day_end: int, now: int
) -> dict:
    # One scan of `listings` for the four flat counts - SUM(CASE WHEN...)
    # rather than four separate queries.
    flat = conn.execute(
        "SELECT "
        "SUM(CASE WHEN gone_at IS NULL THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN first_seen >= ? AND first_seen < ? THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN gone_at >= ? AND gone_at < ? THEN 1 ELSE 0 END), "
        "SUM(CASE WHEN gone_at >= ? AND gone_at < ? AND lifespan_mins IS NULL "
        "THEN 1 ELSE 0 END) "
        "FROM listings WHERE profile_id = ?",
        (day_start, day_end, day_start, day_end, day_start, day_end, profile_id),
    ).fetchone()
    active_listings = flat[0] or 0
    new_listings_today = flat[1] or 0
    deaths_today = flat[2] or 0
    deaths_today_unmeasured = flat[3] or 0

    # A second scan, restricted to the active subset only - GROUP BY,
    # not one CASE branch per known status: spec_status is not read
    # elsewhere in this module, so this stays a plain aggregate rather
    # than hardcoding the status set a second time in the same file (see
    # _ALL_SPEC_STATUSES's own comment on that risk).
    spec_status_counts_active = {status: 0 for status in _ALL_SPEC_STATUSES}
    for status, count in conn.execute(
        "SELECT spec_status, COUNT(*) FROM listings "
        "WHERE profile_id = ? AND gone_at IS NULL GROUP BY spec_status",
        (profile_id,),
    ).fetchall():
        spec_status_counts_active[status] = count

    latest_recorded = conn.execute(
        "SELECT distinct_count, active_count_before, fetched_count FROM sweeps "
        "WHERE profile_id = ? AND sweep_recorded = 1 "
        "ORDER BY swept_at DESC, id DESC LIMIT 1",
        (profile_id,),
    ).fetchone()
    if latest_recorded is None:
        last_sweep_coverage_pct = None
        last_sweep_page_drift = None
        last_sweep_active_count_before = None
    else:
        distinct_count, active_count_before, fetched_count = latest_recorded
        last_sweep_page_drift = fetched_count - distinct_count
        last_sweep_active_count_before = active_count_before
        last_sweep_coverage_pct = (
            distinct_count / active_count_before if active_count_before else None
        )

    return {
        "active_listings": active_listings,
        "new_listings_today": new_listings_today,
        "deaths_today": deaths_today,
        "deaths_today_unmeasured": deaths_today_unmeasured,
        "spec_status_counts_active": spec_status_counts_active,
        "last_sweep_coverage_pct": last_sweep_coverage_pct,
        "last_sweep_page_drift": last_sweep_page_drift,
        "last_sweep_active_count_before": last_sweep_active_count_before,
    }


def _finding(
    conn: sqlite3.Connection, profile_id: str, day_start: int, day_end: int, now: int
) -> dict:
    seven_days_ago = now - 7 * 86400
    one_day_ago = now - 86400

    alert_events_today_live = event_count(
        conn,
        "profile_id = ? AND dry_run = 0 AND sent_at >= ? AND sent_at < ?",
        (profile_id, day_start, day_end),
    )
    alert_events_today_dry = event_count(
        conn,
        "profile_id = ? AND dry_run = 1 AND sent_at >= ? AND sent_at < ?",
        (profile_id, day_start, day_end),
    )
    alert_rows_today = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE profile_id = ? AND sent_at >= ? AND sent_at < ?",
        (profile_id, day_start, day_end),
    ).fetchone()[0]
    alert_events_7d = event_count(
        conn, "profile_id = ? AND sent_at >= ?", (profile_id, seven_days_ago)
    )
    distinct_items_alerted_7d = conn.execute(
        "SELECT COUNT(DISTINCT item_id) FROM alerts WHERE profile_id = ? AND sent_at >= ?",
        (profile_id, seven_days_ago),
    ).fetchone()[0]
    best_ratio_24h = conn.execute(
        "SELECT MIN(ratio_to_p25) FROM alerts WHERE profile_id = ? AND sent_at >= ?",
        (profile_id, one_day_ago),
    ).fetchone()[0]

    delivery_status_row_counts_today: dict[str, int] = {}
    for status, count in conn.execute(
        "SELECT delivery_status, COUNT(*) FROM alerts "
        "WHERE profile_id = ? AND sent_at >= ? AND sent_at < ? "
        "GROUP BY delivery_status",
        (profile_id, day_start, day_end),
    ).fetchall():
        delivery_status_row_counts_today[status] = count

    return {
        "alert_events_today_live": alert_events_today_live,
        "alert_events_today_dry": alert_events_today_dry,
        "alert_rows_today": alert_rows_today,
        "alert_events_7d": alert_events_7d,
        "distinct_items_alerted_7d": distinct_items_alerted_7d,
        "best_ratio_24h": best_ratio_24h,
        "delivery_status_row_counts_today": delivery_status_row_counts_today,
    }


def _baseline(conn: sqlite3.Connection, profile_id: str, now: int) -> dict:
    seven_days_ago = now - 7 * 86400

    baseline_buckets_total = conn.execute(
        "SELECT COUNT(*) FROM baselines WHERE profile_id = ?", (profile_id,)
    ).fetchone()[0]
    baselines_computed_at = conn.execute(
        "SELECT MAX(computed_at) FROM baselines WHERE profile_id = ?", (profile_id,)
    ).fetchone()[0]

    # Scoped to profile_id here, unlike engine.baselines._DEAD_OK_LISTINGS
    # (not profile-scoped) - the two agree while only one profile exists
    # and will diverge the day a second one does.
    dead_spec_ok_count = conn.execute(
        "SELECT COUNT(*) FROM listings "
        "WHERE profile_id = ? AND gone_at IS NOT NULL AND spec_status = ?",
        (profile_id, _OK),
    ).fetchone()[0]
    dead_spec_ok_deaths_7d = conn.execute(
        "SELECT COUNT(*) FROM listings WHERE profile_id = ? AND gone_at IS NOT NULL "
        "AND spec_status = ? AND gone_at >= ?",
        (profile_id, _OK, seven_days_ago),
    ).fetchone()[0]

    # EVENTS, not rows - an alerts-only proxy (see module docstring on why
    # the unbiased, every-scored-listing version isn't computable here).
    baseline_layer_counts_7d: dict[str, int] = {}
    for layer, count in conn.execute(
        "SELECT baseline_layer, COUNT(*) FROM ("
        "  SELECT DISTINCT item_id, sent_at, baseline_layer FROM alerts "
        "  WHERE profile_id = ? AND sent_at >= ?"
        ") GROUP BY baseline_layer",
        (profile_id, seven_days_ago),
    ).fetchall():
        baseline_layer_counts_7d[layer] = count

    return {
        "baseline_buckets_total": baseline_buckets_total,
        "baselines_computed_age_mins": _age_mins(baselines_computed_at, now),
        "dead_spec_ok_count": dead_spec_ok_count,
        "dead_spec_ok_deaths_7d": dead_spec_ok_deaths_7d,
        "baseline_layer_counts_7d": baseline_layer_counts_7d,
    }


def collect_status(
    conn: sqlite3.Connection,
    profile_id: str,
    *,
    ceiling: int | None = None,
    now: int | None = None,
) -> dict:
    """See this module's docstring for the full payload contract, the
    today/7d/24h window definitions, and why each design decision below
    is what it is. `conn` may or may not have `row_factory` set - this
    function and everything it calls reads results positionally and never
    assumes either way.
    """
    now = now if now is not None else int(time.time())
    day_start, day_end = la_day_bounds(now)
    return {
        "alive": _alive(conn, profile_id, day_start, day_end, now, ceiling),
        "collecting": _collecting(conn, profile_id, day_start, day_end, now),
        "finding": _finding(conn, profile_id, day_start, day_end, now),
        "baseline": _baseline(conn, profile_id, now),
    }
