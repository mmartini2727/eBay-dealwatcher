"""Tests for dealwatch.reporting.indicators.build_indicators() (V0.11,
design.md §13).

No database - build_indicators() is a pure function over a plain dict
shaped like collect_status()'s payload, so every test hand-builds that
dict rather than seeding SQLite.

The None-path tests below are the highest-value tests in this file: a
live, healthy system will never exercise these branches on its own (it
always has a real sweep, a real coverage percentage, a real budget row),
so only a mock can prove the "unknown, not ok/warn" contract actually
holds.
"""

from dealwatch.reporting.indicators import (
    BUDGET_WARN_PCT,
    COVERAGE_WARN_PCT,
    PENDING_WARN,
    SWEEP_AGE_WARN_MULTIPLIER,
    build_indicators,
)

SWEEP_INTERVAL = 60


def _status(**overrides):
    status = {
        "alive": {
            "last_sweep_started_age_mins": 5,
            "sweep_bookkeeping_consistent": True,
            "budget": {
                "used": 100,
                "period_is_today": True,
                "ceiling": 4750,
                "remaining": 4650,
            },
        },
        "collecting": {
            "spec_status_counts_active": {
                "ok": 400, "partial": 50, "pending": 5, "stale": 2,
                "rejected": 0, "not_target": 0,
            },
            "last_sweep_coverage_pct": 0.98,
        },
        "finding": {
            "alert_events_today_live": 3,
            "alert_events_today_dry": 0,
            "alert_events_7d": 15,
            "distinct_items_alerted_7d": 12,
            "best_ratio_24h": 0.85,
            "delivery_status_row_counts_today": {"sent": 3},
        },
    }
    for group, fields in overrides.items():
        status[group].update(fields)
    return status


def _build(status, *, dry_run=False, notifiers=("discord",)):
    return build_indicators(
        status,
        sweep_interval_minutes=SWEEP_INTERVAL,
        dry_run=dry_run,
        notifiers=list(notifiers),
    )


# ---------------------------------------------------------------------------
# A fully healthy system
# ---------------------------------------------------------------------------


def test_healthy_status_is_all_ok():
    result = _build(_status())

    assert result["collector"]["state"] == "ok"
    assert result["last_sweep"]["state"] == "ok"
    assert result["sweep_coverage"]["state"] == "ok"
    assert result["bookkeeping"]["state"] == "ok"
    assert result["pending_specs"]["state"] == "ok"
    assert result["stale_specs"]["state"] == "ok"
    assert result["budget"]["state"] == "ok"
    assert result["budget_period"]["state"] == "ok"
    assert result["alerts"]["mode"]["state"] == "ok"
    assert result["alerts"]["notifiers"]["state"] == "ok"
    assert result["alerts"]["delivery_failures_today"]["state"] == "ok"


# ---------------------------------------------------------------------------
# None paths -> unknown, never ok/warn (the highest-value test in this
# milestone - see module docstring).
# ---------------------------------------------------------------------------


def test_last_sweep_is_unknown_not_ok_when_age_is_none():
    result = _build(_status(alive={"last_sweep_started_age_mins": None}))
    assert result["last_sweep"]["state"] == "unknown"


def test_sweep_coverage_is_unknown_not_warn_when_pct_is_none():
    result = _build(_status(collecting={"last_sweep_coverage_pct": None}))
    assert result["sweep_coverage"]["state"] == "unknown"


def test_bookkeeping_is_unknown_not_warn_when_none():
    result = _build(_status(alive={"sweep_bookkeeping_consistent": None}))
    assert result["bookkeeping"]["state"] == "unknown"


def test_budget_is_unknown_when_ceiling_is_none():
    status = _status()
    status["alive"]["budget"]["ceiling"] = None
    result = _build(status)
    assert result["budget"]["state"] == "unknown"


def test_budget_is_unknown_when_used_is_none():
    # An empty `budget` table (a database the collector has never run
    # against) reports used=None with ceiling still present - a
    # percentage needs both operands, so this must not silently divide
    # None or treat it as zero usage.
    status = _status()
    status["alive"]["budget"]["used"] = None
    result = _build(status)
    assert result["budget"]["state"] == "unknown"


def test_budget_period_is_unknown_when_none():
    status = _status()
    status["alive"]["budget"]["period_is_today"] = None
    result = _build(status)
    assert result["budget_period"]["state"] == "unknown"


def test_best_ratio_24h_is_unknown_not_a_fabricated_number_when_none():
    result = _build(_status(finding={"best_ratio_24h": None}))
    assert result["alerts"]["best_ratio_24h"]["state"] == "unknown"
    assert result["alerts"]["best_ratio_24h"]["value"] is None


# ---------------------------------------------------------------------------
# Rollup: unknown outranks ok but not warn
# ---------------------------------------------------------------------------


def test_rollup_is_unknown_when_one_component_is_unknown_and_none_warn():
    result = _build(_status(collecting={"last_sweep_coverage_pct": None}))
    assert result["sweep_coverage"]["state"] == "unknown"
    assert result["last_sweep"]["state"] == "ok"
    assert result["bookkeeping"]["state"] == "ok"
    assert result["collector"]["state"] == "unknown"


def test_rollup_is_warn_when_one_component_warns_even_if_another_is_unknown():
    result = _build(
        _status(
            alive={
                "last_sweep_started_age_mins": SWEEP_AGE_WARN_MULTIPLIER * SWEEP_INTERVAL + 1,
                "sweep_bookkeeping_consistent": None,
            }
        )
    )
    assert result["last_sweep"]["state"] == "warn"
    assert result["bookkeeping"]["state"] == "unknown"
    assert result["collector"]["state"] == "warn"


def test_rollup_is_ok_only_when_all_three_are_ok():
    result = _build(_status())
    assert result["collector"]["state"] == "ok"


# ---------------------------------------------------------------------------
# Threshold boundaries
# ---------------------------------------------------------------------------


def test_last_sweep_exactly_at_threshold_is_ok():
    result = _build(
        _status(alive={"last_sweep_started_age_mins": SWEEP_AGE_WARN_MULTIPLIER * SWEEP_INTERVAL})
    )
    assert result["last_sweep"]["state"] == "ok"


def test_last_sweep_one_minute_past_threshold_warns():
    result = _build(
        _status(
            alive={"last_sweep_started_age_mins": SWEEP_AGE_WARN_MULTIPLIER * SWEEP_INTERVAL + 1}
        )
    )
    assert result["last_sweep"]["state"] == "warn"


def test_sweep_coverage_exactly_at_threshold_is_ok():
    result = _build(_status(collecting={"last_sweep_coverage_pct": COVERAGE_WARN_PCT / 100}))
    assert result["sweep_coverage"]["state"] == "ok"


def test_sweep_coverage_just_below_threshold_warns():
    result = _build(
        _status(collecting={"last_sweep_coverage_pct": (COVERAGE_WARN_PCT - 0.1) / 100})
    )
    assert result["sweep_coverage"]["state"] == "warn"


def test_pending_specs_exactly_at_threshold_is_ok():
    status = _status()
    status["collecting"]["spec_status_counts_active"]["pending"] = PENDING_WARN
    result = _build(status)
    assert result["pending_specs"]["state"] == "ok"


def test_pending_specs_one_above_threshold_warns():
    status = _status()
    status["collecting"]["spec_status_counts_active"]["pending"] = PENDING_WARN + 1
    result = _build(status)
    assert result["pending_specs"]["state"] == "warn"


def test_budget_exactly_at_threshold_warns():
    status = _status()
    status["alive"]["budget"]["ceiling"] = 100
    status["alive"]["budget"]["used"] = BUDGET_WARN_PCT  # used/ceiling*100 == BUDGET_WARN_PCT
    result = _build(status)
    assert result["budget"]["state"] == "warn"


# ---------------------------------------------------------------------------
# Alerts section
# ---------------------------------------------------------------------------


def test_dry_run_mode_warns():
    result = _build(_status(), dry_run=True)
    assert result["alerts"]["mode"]["state"] == "warn"
    assert result["alerts"]["mode"]["value"] == "dry run"


def test_empty_notifiers_warns():
    result = _build(_status(), notifiers=[])
    assert result["alerts"]["notifiers"]["state"] == "warn"


def test_delivery_failures_warn_when_nonzero():
    result = _build(_status(finding={"delivery_status_row_counts_today": {"failed": 1}}))
    assert result["alerts"]["delivery_failures_today"]["state"] == "warn"


def test_alerts_today_is_info_with_live_and_dry_split():
    result = _build(
        _status(finding={"alert_events_today_live": 4, "alert_events_today_dry": 2})
    )
    assert result["alerts"]["alerts_today"]["state"] == "info"
    assert result["alerts"]["alerts_today"]["value"] == "4 live / 2 dry"
