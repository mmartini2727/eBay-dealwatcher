"""Assembles the V0.11 dashboard's full payload in one call (design.md
§13): status, health indicators, and the panel queries, each isolated so
one failing section cannot take the rest of the page down with it - the
moment you most want this page to render is the moment a query is most
likely to blow up (same posture as engine/alerting.py's per-notifier
isolation).

get_payload() adds a short TTL cache on top of build_payload(), because
collect_status() alone runs 17 full-scan-equivalent queries against tables
that keep growing (reporting/status.py's docstring, design.md §12's
live-verification addendum), and a `<meta http-equiv="refresh">` page
re-running that on every reload, from every open tab, is exactly the load
pattern that turns "small today" into a real cost (design.md §13's
"Decision" on caching vs. indexing - this module takes the caching half of
that choice; the missing profile_id-leading indexes are migration 8 in
storage/sqlite.py, taken separately).

V0.11a addendum: a database-unavailable failure (connect_readonly() itself
raising) is now ONE payload-level signal (`database_error`), not six
per-section {"error": ...} boxes - see _database_unavailable_payload()'s
own docstring. It also gets its own, much shorter cache TTL
(_DATABASE_ERROR_TTL_SECONDS) than a normal payload - see get_payload().
"""

import logging
import sqlite3
import threading
import time

from dealwatch.reporting import panels
from dealwatch.reporting.indicators import build_budget_pacing, build_indicators
from dealwatch.reporting.status import collect_status
from dealwatch.storage.sqlite import connect_readonly

logger = logging.getLogger(__name__)


def _safe(name: str, fn):
    """Run one payload section, isolated. A raised exception is logged and
    replaced with an {"error": ...} dict rather than propagating - the
    other sections must still build. Living here, in tested Python, rather
    than in the future template (prompt 2's job), is the point: a template
    is the wrong place to discover a query regressed.
    """
    try:
        return fn()
    except Exception as exc:
        logger.exception("dashboard section %r failed", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


def _budget_ceiling_display(ceiling, daily_call_limit, daily_reserve_calls) -> str | None:
    """"4750 usable (5000 − 250 reserved)" (V0.11a C1) - the reserve is
    the thing worth remembering exists on the day the count is near the
    limit, and a bare "4750" doesn't say it's there at all. Built from
    Settings' own two numbers, passed through unchanged - never re-derives
    `ceiling` by subtracting them again here (main.py already computed it
    once, the same way providers/ratelimit.py's DailyBudget does)."""
    if ceiling is None or daily_call_limit is None or daily_reserve_calls is None:
        return None
    return f"{ceiling} usable ({daily_call_limit} − {daily_reserve_calls} reserved)"


def build_payload(
    conn,
    *,
    profile_id: str,
    sweep_interval_minutes: int,
    dry_run: bool,
    notifiers: list[str],
    ceiling: int | None = None,
    daily_call_limit: int | None = None,
    daily_reserve_calls: int | None = None,
    min_samples: int = 12,
    now: int | None = None,
) -> dict:
    now = now if now is not None else int(time.time())

    status = _safe("status", lambda: collect_status(conn, profile_id, ceiling=ceiling, now=now))

    def _build_indicators():
        if isinstance(status, dict) and "error" in status:
            # indicators.py is a pure function OVER the status payload - if
            # that payload itself failed, there is nothing to derive
            # indicators from. Raising here (rather than fabricating a
            # status dict) routes this section through the same
            # {"error": ...} shape as any other failed section, instead of
            # inventing a second failure representation.
            raise RuntimeError("status section failed; indicators skipped")
        return build_indicators(
            status,
            sweep_interval_minutes=sweep_interval_minutes,
            dry_run=dry_run,
            notifiers=notifiers,
        )

    def _build_pacing():
        # Same dependency shape as indicators: build_budget_pacing() is a
        # pure function over `status`, so a failed status leaves it
        # nothing to compute from.
        if isinstance(status, dict) and "error" in status:
            raise RuntimeError("status section failed; budget pacing skipped")
        return build_budget_pacing(status, now)

    return {
        "generated_at": now,
        "profile_id": profile_id,
        # None here, always (never a string) - the connection succeeded,
        # or build_payload() wouldn't be running at all. See
        # _database_unavailable_payload() for the other case.
        "database_error": None,
        "budget_ceiling_display": _budget_ceiling_display(
            ceiling, daily_call_limit, daily_reserve_calls
        ),
        "status": status,
        "indicators": _safe("indicators", _build_indicators),
        "budget_pacing": _safe("budget_pacing", _build_pacing),
        "alerts_per_day": _safe(
            "alerts_per_day", lambda: panels.alerts_per_day(conn, profile_id, now=now)
        ),
        "recent_alerts": _safe("recent_alerts", lambda: panels.recent_alerts(conn, profile_id)),
        "recent_listings": _safe(
            "recent_listings", lambda: panels.recent_listings(conn, profile_id)
        ),
        # Not in design.md §13's original payload sketch, added here rather
        # than left for the template to fetch separately: get_payload()'s
        # entire point is one cached round-trip per render, and a template
        # calling panels.baseline_coverage() itself on every request would
        # bypass that cache for exactly this one panel.
        "baseline_coverage": _safe(
            "baseline_coverage", lambda: panels.baseline_coverage(conn, profile_id, now=now)
        ),
        "computed_baselines": _safe(
            "computed_baselines", lambda: panels.computed_baselines(conn, profile_id)
        ),
        "baseline_queue": _safe(
            "baseline_queue",
            lambda: panels.baseline_queue(conn, profile_id, min_samples=min_samples),
        ),
    }


_cache_lock = threading.Lock()
# db_path (str) -> (stored_at [time.monotonic()], kwargs used, payload,
# the TTL that applies to THIS entry). The TTL travels with the entry
# rather than always being re-read from the current call's ttl_seconds
# argument, because a database-unavailable payload (V0.11a Part A) is
# deliberately cached for a much shorter window than a normal one - see
# _DATABASE_ERROR_TTL_SECONDS. Not an lru_cache: lru_cache has no expiry,
# and would serve one render's payload forever once computed.
# time.monotonic(), not time.time(), for the stored clock - immune to
# wall-clock adjustments, which a TTL gate must be.
_cache: dict[str, tuple[float, dict, dict, int]] = {}

_PAYLOAD_SECTIONS = (
    "status", "indicators", "budget_pacing", "alerts_per_day", "recent_alerts",
    "recent_listings", "baseline_coverage", "computed_baselines", "baseline_queue",
)

# A transient connection failure (a bind mount reattaching, a snapshot
# restore in progress, a container race on first boot) recovering mid-
# render must not still read as "database unavailable" for a stale 30s
# afterward - design.md §13's V0.11a Part A is explicit that this is a
# worse experience than the failure itself. Cached far shorter than a
# normal payload (`ttl_seconds`, 30s in main.py) for exactly that reason.
_DATABASE_ERROR_TTL_SECONDS = 5


def _database_unavailable_payload(now: int, exc: Exception, kwargs: dict) -> dict:
    """ONE banner, not six per-panel boxes (V0.11a Part A).

    connect_readonly() itself failing - no db file yet, a detached bind
    mount, a bad path after a restore - is one root cause. Filling every
    section with its own {"error": ...} (the shape _safe() produces for an
    individual query failure) made six IDENTICAL failures read as six
    unrelated cosmetic faults instead of naming the actual problem: the
    database is gone. `database_error` is the one signal the template
    checks first; when it's set, every panel is suppressed in favor of a
    single page-spanning banner (see dashboard.html).

    profile_id and budget_ceiling_display need no query at all - they come
    straight from `kwargs`/Settings - so they're still populated here: the
    header and the budget-ceiling note render even with the database gone;
    only the DATA panels are replaced by the banner.
    """
    return {
        "generated_at": now,
        "profile_id": kwargs.get("profile_id"),
        "database_error": f"{type(exc).__name__}: {exc}",
        "budget_ceiling_display": _budget_ceiling_display(
            kwargs.get("ceiling"), kwargs.get("daily_call_limit"), kwargs.get("daily_reserve_calls")
        ),
        **{section: None for section in _PAYLOAD_SECTIONS},
    }


def get_payload(db_path, *, ttl_seconds: int = 30, **kwargs) -> dict:
    """build_payload()'s result, cached for ttl_seconds keyed on db_path
    and the exact kwargs used to build it - a config change (e.g. dry_run
    flipping) invalidates the cache rather than silently serving a payload
    built under different settings.

    A cache hit returns the stored payload completely unchanged, including
    its original `generated_at` - the age shown on the page must be the
    age of the data, not of the render that happened to ask for it.

    The lock is held across a cache miss's rebuild, not just the read/write
    around it: request handlers run in the anyio threadpool, so several
    renders can miss at once on a cold cache, and serializing them behind
    one lock means one rebuild instead of a thundering herd of identical
    queries against the same database at the same moment.

    Only `sqlite3.OperationalError` (V0.11a Part A) is treated as "the
    database is unavailable" - the realistic shape of "no file yet,"
    "database is locked," or similar, and exactly what connect_readonly()
    was observed raising in live-Docker verification. A blanket `except
    Exception` here would also catch a real bug in this function's own
    code (a bad db_path type, an AttributeError from a future refactor)
    and mislabel it as "database unavailable" - which would be actively
    misleading in the one place on this page a maintainer most needs an
    honest error.
    """
    key = str(db_path)

    with _cache_lock:
        now_mono = time.monotonic()
        cached = _cache.get(key)
        if cached is not None:
            stored_at, cached_kwargs, payload, effective_ttl = cached
            if now_mono - stored_at < effective_ttl and cached_kwargs == kwargs:
                return payload

        try:
            conn = connect_readonly(db_path)
        except sqlite3.OperationalError as exc:
            logger.exception("could not open %r for the dashboard", db_path)
            now = kwargs.get("now")
            payload = _database_unavailable_payload(
                now if now is not None else int(time.time()), exc, kwargs
            )
            effective_ttl = _DATABASE_ERROR_TTL_SECONDS
        else:
            try:
                payload = build_payload(conn, **kwargs)
            finally:
                conn.close()
            effective_ttl = ttl_seconds

        _cache[key] = (now_mono, kwargs, payload, effective_ttl)
        return payload
