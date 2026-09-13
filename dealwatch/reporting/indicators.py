"""Pure health-indicator derivation over collect_status()'s payload (V0.11,
design.md §13). No database access, no I/O - build_indicators() is a
function of its inputs only, so every branch is testable without a
connection.

Each indicator is {"state", "label", "value", "group"}, where state is one
of:
  - "ok"      - evaluated, healthy
  - "warn"    - evaluated, not healthy
  - "unknown" - not evaluable, distinct from unhealthy
  - "info"    - no health judgment; display the value only

build_budget_pacing() (V0.11a Part C) is a second, separate pure function
in this module - it doesn't produce an indicator (no ok/warn/unknown
state; it's a pair of comparable 0..1 fractions for two stacked bars, not
a health check) but has the exact same "pure function over collect_
status()'s payload, no I/O" shape as build_indicators() itself, so it
lives here rather than starting a third module for one function.

and group is one of "health" | "alerts" | "baseline" - which panel section
the template renders the indicator under. build_indicators() returns one
flat dict, not the health/alerts split an earlier draft had: a nested
"alerts" sub-dict meant any single loop over the payload (the template's
whole rendering strategy) hit a KeyError the moment it reached that key.
Flat, tagged with `group`, lets one loop render every indicator and filter
by tag - insertion order is preserved (health, then alerts, then baseline),
so a template that renders in dict order still groups correctly without
sorting.

The "unknown" state is this module's entire reason for existing separately
from a template that just eyeballs collect_status()'s dict. collect_status()
deliberately returns None for fields that are genuinely unevaluable yet
(sweep_bookkeeping_consistent with no recorded sweep, last_sweep_coverage_pct
with no active_count_before, any *_age_mins with no timestamp) rather than
guessing - see reporting/status.py's own docstring on that discipline.
Collapsing a None here to "ok" or "warn" would throw that distinction away
one layer up, and a green checkmark on a container that has never swept is
the worst thing this page could show. Every None path in this module must
resolve to "unknown", never "ok" or "warn" - tests/test_indicators.py checks
this explicitly, branch by branch, because a healthy live system will never
exercise these paths on its own to prove it.
"""

from dealwatch.providers.ratelimit import la_day_bounds

# Multiplier on the profile's own sweep_interval_minutes, not a fixed
# minute count - profiles/thinkpad-t14.yaml's poll.sweep_interval_minutes
# is 60 today, but a different profile could run a slower cadence
# legitimately, and a fixed absolute threshold would false-positive "warn"
# against it. This is the one threshold design.md §13 calls out as
# genuinely per-profile rather than a global constant.
SWEEP_AGE_WARN_MULTIPLIER = 2

# Matches engine/collector.py's own _COVERAGE_WARNING_RATIO (0.95, as a
# fraction there; expressed as a percent here since last_sweep_coverage_pct
# is a 0..1 fraction and the dashboard displays a percent). run_sweep_cycle
# already logs a WARNING at this exact threshold - the dashboard must not
# invent a second coverage threshold that could silently drift from it.
COVERAGE_WARN_PCT = 95.0

# A handful of pending/stale specs is the normal gap between an insert and
# the collector's next normalize pass, not a stall - CLAUDE.md's own
# "sanity-floor queue is a to-do list" framing applies here too: a small
# number is expected traffic, not an incident. No real measurement backs
# this exact number; it is a round "more than a few" guess pending real
# volume data, same honesty as the seed-chart gaps CLAUDE.md's Open Items
# already carries for a different threshold.
PENDING_WARN = 25
STALE_WARN = 25

# A deliberately conservative early-warning line, well ahead of
# Settings.daily_reserve_calls (design.md §7's 250-call headroom baked into
# `ceiling` itself) - this is a dashboard-only heads-up that today's usage
# is trending toward the ceiling, not a hard cutoff; the real cutoff is
# `ceiling` itself, enforced by providers/ratelimit.py, not by this module.
BUDGET_WARN_PCT = 80.0

# 48 hours. A judgment call, not a derived number: anchored on the observed
# dead spec_status='ok' death rate (~279 over 7 days at the time this was
# written, design.md §13's live-verification addendum) - baselines going
# stale for two full days while listings keep dying at that rate means the
# survival pool has moved meaningfully since the last recompute without
# anyone knowing. scripts/recompute_baselines.py is manual-only (no cron,
# no collector hook) - this indicator is the only thing that will ever
# surface that drift; nothing else in the system watches this age at all.
BASELINE_STALE_WARN_MINS = 2880


def _indicator(state: str, label: str, value, group: str) -> dict:
    return {"state": state, "label": label, "value": value, "group": group}


def _threshold_indicator(value, warn_above, label, group, *, fmt=str) -> dict:
    """ok/warn/unknown for "a count that should stay below a ceiling."
    `value` is None exactly when the underlying metric is unevaluable."""
    if value is None:
        return _indicator("unknown", label, None, group)
    state = "warn" if value > warn_above else "ok"
    return _indicator(state, label, fmt(value), group)


# state -> the word the rollup's value renders, so the template never
# invents "healthy"/"degraded" itself from a bare ok/warn/unknown state.
_ROLLUP_VALUE = {"ok": "healthy", "warn": "degraded", "unknown": "unknown"}


def build_indicators(
    status: dict,
    *,
    sweep_interval_minutes: int,
    dry_run: bool,
    notifiers: list[str],
) -> dict:
    alive = status["alive"]
    collecting = status["collecting"]
    finding = status["finding"]
    baseline = status["baseline"]

    # --- collector health ---------------------------------------------

    last_sweep_age = alive["last_sweep_started_age_mins"]
    if last_sweep_age is None:
        last_sweep = _indicator("unknown", "Last sweep", None, "health")
    else:
        warn_above = SWEEP_AGE_WARN_MULTIPLIER * sweep_interval_minutes
        state = "warn" if last_sweep_age > warn_above else "ok"
        last_sweep = _indicator(state, "Last sweep", f"{last_sweep_age} min ago", "health")

    coverage_pct = collecting["last_sweep_coverage_pct"]
    if coverage_pct is None:
        sweep_coverage = _indicator("unknown", "Sweep coverage", None, "health")
    else:
        coverage_pct_display = coverage_pct * 100
        state = "ok" if coverage_pct_display >= COVERAGE_WARN_PCT else "warn"
        sweep_coverage = _indicator(
            state, "Sweep coverage", f"{coverage_pct_display:.1f}%", "health"
        )

    consistent = alive["sweep_bookkeeping_consistent"]
    if consistent is None:
        bookkeeping = _indicator("unknown", "Bookkeeping", None, "health")
    else:
        bookkeeping = _indicator(
            "ok" if consistent else "warn",
            "Bookkeeping",
            "consistent" if consistent else "mismatch",
            "health",
        )

    # Rollup over exactly these three - "unknown" outranks "ok" but not
    # "warn": a component that can't be evaluated is not the same as a
    # clean bill of health, but it also isn't itself an active problem the
    # way "warn" is. Deliberately does NOT include baselines_age (A5,
    # below): this rollup answers "is the collector alive," baselines_age
    # answers "is scoring current" - conflating them would let one mask
    # the other on the one indicator most likely to get glanced at.
    _RANK = {"ok": 0, "unknown": 1, "warn": 2}
    worst = max((last_sweep, sweep_coverage, bookkeeping), key=lambda ind: _RANK[ind["state"]])
    collector_rollup = _indicator(
        worst["state"], "Collector", _ROLLUP_VALUE[worst["state"]], "health"
    )

    pending_specs = _threshold_indicator(
        collecting["spec_status_counts_active"].get("pending", 0),
        PENDING_WARN,
        "Pending specs",
        "health",
    )
    stale_specs = _threshold_indicator(
        collecting["spec_status_counts_active"].get("stale", 0),
        STALE_WARN,
        "Stale specs",
        "health",
    )

    budget = alive["budget"]
    ceiling, used = budget["ceiling"], budget["used"]
    # unknown when ceiling is None (explicit in design.md §13's rule table)
    # AND when used is None (an empty budget table, e.g. a database the
    # collector has never run against - test_status.py's own empty-database
    # case) - a percentage needs both operands, and guessing either one
    # produces exactly the false "ok" this module exists to prevent.
    if ceiling is None or used is None:
        budget_indicator = _indicator("unknown", "Budget", None, "health")
    else:
        used_pct = used / ceiling * 100 if ceiling else 100.0
        # design.md's rule table says "ok below BUDGET_WARN_PCT; warn
        # above" without pinning the exact boundary - treated as warn at
        # or above the threshold (closed-open, matching the sanity-floor
        # convention in engine/scoring.py: the boundary itself already
        # counts as crossed, not as the last safe value).
        state = "warn" if used_pct >= BUDGET_WARN_PCT else "ok"
        budget_indicator = _indicator(
            state, "Budget", f"{used} / {ceiling} ({used_pct:.0f}%)", "health"
        )

    period_is_today = budget["period_is_today"]
    if period_is_today is None:
        budget_period = _indicator("unknown", "Budget period", None, "health")
    else:
        # The date, not just the word (V0.11a's C3 amendment) - "current"
        # means nothing on its own; period_is_today is only ever non-None
        # when `period` itself is too (status.py's own budget_row is-None
        # branch sets both together), so no separate None-guard is needed
        # for `period` here.
        period = budget["period"]
        value = f"current ({period})" if period_is_today else f"stale ({period})"
        budget_period = _indicator(
            "ok" if period_is_today else "warn", "Budget period", value, "health"
        )

    # --- baseline -------------------------------------------------------

    baselines_age_mins = baseline["baselines_computed_age_mins"]
    if baselines_age_mins is None:
        baselines_age = _indicator("unknown", "Baselines age", None, "baseline")
    else:
        state = "warn" if baselines_age_mins > BASELINE_STALE_WARN_MINS else "ok"
        baselines_age = _indicator(
            state, "Baselines age", f"{baselines_age_mins} min ago", "baseline"
        )

    # --- alerts -----------------------------------------------------

    mode = _indicator("warn" if dry_run else "ok", "Mode", "dry run" if dry_run else "live", "alerts")

    notifiers_indicator = _indicator(
        "warn" if not notifiers else "ok", "Notifiers", ", ".join(notifiers), "alerts"
    )

    failed_today = finding["delivery_status_row_counts_today"].get("failed", 0)
    delivery_failures = _indicator(
        "ok" if failed_today == 0 else "warn",
        "Delivery failures today",
        str(failed_today),
        "alerts",
    )

    live = finding["alert_events_today_live"]
    dry = finding["alert_events_today_dry"]
    # "(PT)" explicit, not just "today" (V0.11's C4 requirement): "today"
    # and "24h" are different windows that can legitimately disagree (an
    # alert fired late yesterday evening sits in a rolling 24h window but
    # not in today's LA-calendar window) - the live data has hit exactly
    # this case (best_ratio_24h=0.86 next to alerts_today=0). Proximity in
    # a layout must never be left to imply a shared window.
    alerts_today = _indicator("info", "Alerts today (PT)", f"{live} live / {dry} dry", "alerts")

    alerts_7d = _indicator("info", "Alerts (7d)", str(finding["alert_events_7d"]), "alerts")

    distinct_items_7d = _indicator(
        "info", "Distinct items alerted (7d)", str(finding["distinct_items_alerted_7d"]), "alerts"
    )

    best_ratio = finding["best_ratio_24h"]
    if best_ratio is None:
        # No alerts in the window is not the same as "found a 0.0 ratio
        # deal" - unknown, not a fabricated best-case or worst-case number.
        best_ratio_24h = _indicator("unknown", "Best ratio (24h)", None, "alerts")
    else:
        best_ratio_24h = _indicator("info", "Best ratio (24h)", f"{best_ratio:.2f}", "alerts")

    return {
        "collector": collector_rollup,
        "last_sweep": last_sweep,
        "sweep_coverage": sweep_coverage,
        "bookkeeping": bookkeeping,
        "pending_specs": pending_specs,
        "stale_specs": stale_specs,
        "budget": budget_indicator,
        "budget_period": budget_period,
        "baselines_age": baselines_age,
        "mode": mode,
        "notifiers": notifiers_indicator,
        "delivery_failures_today": delivery_failures,
        "alerts_today": alerts_today,
        "alerts_7d": alerts_7d,
        "distinct_items_alerted_7d": distinct_items_7d,
        "best_ratio_24h": best_ratio_24h,
    }


def build_budget_pacing(status: dict, now: int) -> dict:
    """Two comparable 0..1 fractions for the budget-pacing bars (V0.11a
    Part C2): how much of today's eBay call budget is used, and how much
    of today's LA calendar day has already elapsed. Day-elapsed comes from
    la_day_bounds(now), never a UTC hour - providers/ratelimit.py's
    la_day_bounds() exists specifically because that mismatch is a real,
    previously-hit bug class in this project, not a hypothetical one.

    Both come back pre-scaled to percent and pre-formatted, not just the
    raw fraction: the template's only job is to print `*_pct` as a CSS
    width and `*_pct_display` as text, with no arithmetic of its own -
    same discipline as every `*_display` field elsewhere in this module
    and in reporting/panels.py.

    No projected end-of-day total, deliberately: a linear extrapolation
    from, say, 00:20 PT is noise, and a number that's garbage every
    morning is a number that gets learned to ignore, taking the rest of
    the panel with it. "Is the budget bar shorter than the day bar" is
    the entire question these two fractions answer.
    """
    budget = status["alive"]["budget"]
    ceiling, used = budget["ceiling"], budget["used"]
    if ceiling is None or used is None or ceiling == 0:
        budget_fraction = None
    else:
        # Clamped at 1.0 - `used` can exceed `ceiling` (the reserve exists
        # precisely so `ceiling` is not the hard stop; providers/
        # ratelimit.py's real budget enforcement is against
        # daily_call_limit, not this dashboard-only ceiling), and a bar
        # wider than its own track is a rendering bug, not information.
        budget_fraction = min(used / ceiling, 1.0)

    day_start, day_end = la_day_bounds(now)
    day_fraction = (now - day_start) / (day_end - day_start)

    return {
        "budget_pct": None if budget_fraction is None else round(budget_fraction * 100, 1),
        "budget_pct_display": (
            "unknown" if budget_fraction is None else f"{budget_fraction * 100:.0f}%"
        ),
        "day_pct": round(day_fraction * 100, 1),
        "day_pct_display": f"{day_fraction * 100:.0f}%",
    }
