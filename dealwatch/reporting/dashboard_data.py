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
"""

import logging
import threading
import time

from dealwatch.reporting import panels
from dealwatch.reporting.indicators import build_indicators
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


def build_payload(
    conn,
    *,
    profile_id: str,
    sweep_interval_minutes: int,
    dry_run: bool,
    notifiers: list[str],
    ceiling: int | None = None,
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

    return {
        "generated_at": now,
        "status": status,
        "indicators": _safe("indicators", _build_indicators),
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
    }


_cache_lock = threading.Lock()
# db_path (str) -> (stored_at [time.monotonic()], kwargs used, payload).
# Not an lru_cache: lru_cache has no expiry, and would serve one render's
# payload forever once computed. time.monotonic(), not time.time(), for
# the stored clock - immune to wall-clock adjustments, which a TTL gate
# must be.
_cache: dict[str, tuple[float, dict, dict]] = {}

_PAYLOAD_SECTIONS = (
    "status", "indicators", "alerts_per_day", "recent_alerts",
    "recent_listings", "baseline_coverage",
)


def _connection_failed_payload(now: int, exc: Exception) -> dict:
    """Every section as its own {"error": ...}, same shape _safe() produces
    per-section - for the one failure mode that happens BEFORE any section
    gets a chance to run at all. Found by live Docker verification, not
    theorized: on a container that has never had a writer create data/
    dealwatch.db yet (no collector started, /health never hit either -
    exactly the credentials-missing case B1 is about), connect_readonly()
    itself raises "unable to open database file" - mode=ro correctly
    refuses to create the file, which is the whole point of that
    function, but that refusal happened outside build_payload()'s
    per-section try/except, so the route 500'd instead of rendering. This
    keeps the "always return a renderable payload" contract from
    get_payload() outward, not just from build_payload() outward.
    """
    return {
        "generated_at": now,
        **{
            section: {"error": f"{type(exc).__name__}: {exc}"}
            for section in _PAYLOAD_SECTIONS
        },
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
    """
    key = str(db_path)

    with _cache_lock:
        now_mono = time.monotonic()
        cached = _cache.get(key)
        if cached is not None:
            stored_at, cached_kwargs, payload = cached
            if now_mono - stored_at < ttl_seconds and cached_kwargs == kwargs:
                return payload

        try:
            conn = connect_readonly(db_path)
        except Exception as exc:
            logger.exception("could not open %r for the dashboard", db_path)
            now = kwargs.get("now")
            payload = _connection_failed_payload(
                now if now is not None else int(time.time()), exc
            )
        else:
            try:
                payload = build_payload(conn, **kwargs)
            finally:
                conn.close()

        _cache[key] = (now_mono, kwargs, payload)
        return payload
