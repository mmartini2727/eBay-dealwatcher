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

build_indicators() returns one flat dict (V0.11's A3 amendment) - every
indicator, health/alerts/baseline alike, is a top-level key tagged with a
"group". Tests index results directly (result["mode"], not
result["alerts"]["mode"]).
"""

from datetime import datetime, timezone

import pytest

from dealwatch.reporting.indicators import (
    BASELINE_STALE_WARN_MINS,
    BUDGET_WARN_PCT,
    COVERAGE_WARN_PCT,
    PENDING_WARN,
    SWEEP_AGE_WARN_MULTIPLIER,
    build_budget_pacing,
    build_indicators,
)

SWEEP_INTERVAL = 60


def _status(**overrides):
    status = {
        "alive": {
            "last_sweep_started_age_mins": 5,
            "sweep_bookkeeping_consistent": True,
            "budget": {
                "period": "2026-09-12",
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
        "baseline": {
            "baselines_computed_age_mins": 60,
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
    assert result["baselines_age"]["state"] == "ok"
    assert result["mode"]["state"] == "ok"
    assert result["notifiers"]["state"] == "ok"
    assert result["delivery_failures_today"]["state"] == "ok"


# ---------------------------------------------------------------------------
# Flattening (A3): every indicator is top-level and tagged with its group
# ---------------------------------------------------------------------------


def test_indicators_are_flat_not_nested():
    result = _build(_status())
    assert "alerts" not in result  # no nested sub-dict left over
    assert isinstance(result["mode"], dict)
    assert isinstance(result["last_sweep"], dict)


def test_every_indicator_is_tagged_with_its_group():
    result = _build(_status())

    health_keys = {
        "collector", "last_sweep", "sweep_coverage", "bookkeeping",
        "pending_specs", "stale_specs", "budget", "budget_period",
    }
    alerts_keys = {
        "mode", "notifiers", "delivery_failures_today", "alerts_today",
        "alerts_7d", "distinct_items_alerted_7d", "best_ratio_24h",
    }
    baseline_keys = {"baselines_age"}

    for key in health_keys:
        assert result[key]["group"] == "health", key
    for key in alerts_keys:
        assert result[key]["group"] == "alerts", key
    for key in baseline_keys:
        assert result[key]["group"] == "baseline", key


def test_group_order_is_stable_health_then_baseline_then_alerts():
    # Not a requirement that groups be contiguous - only that a template
    # filtering by group sees a stable order within that group. This also
    # pins the actual current layout so a future reordering is a visible
    # diff, not a silent shuffle.
    result = _build(_status())
    keys = list(result.keys())
    health_order = [k for k in keys if result[k]["group"] == "health"]
    assert health_order == [
        "collector", "last_sweep", "sweep_coverage", "bookkeeping",
        "pending_specs", "stale_specs", "budget", "budget_period",
    ]


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
    assert result["best_ratio_24h"]["state"] == "unknown"
    assert result["best_ratio_24h"]["value"] is None


def test_baselines_age_is_unknown_when_none():
    result = _build(_status(baseline={"baselines_computed_age_mins": None}))
    assert result["baselines_age"]["state"] == "unknown"
    assert result["baselines_age"]["value"] is None


# ---------------------------------------------------------------------------
# Rollup: unknown outranks ok but not warn; baselines_age is excluded
# ---------------------------------------------------------------------------


def test_rollup_is_unknown_when_one_component_is_unknown_and_none_warn():
    result = _build(_status(collecting={"last_sweep_coverage_pct": None}))
    assert result["sweep_coverage"]["state"] == "unknown"
    assert result["last_sweep"]["state"] == "ok"
    assert result["bookkeeping"]["state"] == "ok"
    assert result["collector"]["state"] == "unknown"
    assert result["collector"]["value"] == "unknown"


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
    assert result["collector"]["value"] == "degraded"


def test_rollup_is_ok_only_when_all_three_are_ok():
    result = _build(_status())
    assert result["collector"]["state"] == "ok"
    assert result["collector"]["value"] == "healthy"


def test_rollup_ignores_baselines_age_being_stale():
    # A5: the rollup answers "is the collector alive," not "is scoring
    # current" - a stale baseline must not degrade the collector rollup.
    result = _build(
        _status(baseline={"baselines_computed_age_mins": BASELINE_STALE_WARN_MINS + 1})
    )
    assert result["baselines_age"]["state"] == "warn"
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


def test_sweep_coverage_at_0_90_warns():
    # A7: the current suite only exercised coverage values above threshold
    # (ok is trivially consistent with either a correctly scaled comparison
    # OR a lucky one that happens to still clear 95%). 0.90 is a real,
    # unambiguously-below-threshold fraction - only this proves the *100
    # scaling and the >= direction are both actually live, not just
    # coincidentally passing.
    result = _build(_status(collecting={"last_sweep_coverage_pct": 0.90}))
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


def test_baselines_age_exactly_at_threshold_is_ok():
    result = _build(_status(baseline={"baselines_computed_age_mins": BASELINE_STALE_WARN_MINS}))
    assert result["baselines_age"]["state"] == "ok"


def test_baselines_age_one_minute_past_threshold_warns():
    result = _build(
        _status(baseline={"baselines_computed_age_mins": BASELINE_STALE_WARN_MINS + 1})
    )
    assert result["baselines_age"]["state"] == "warn"


# ---------------------------------------------------------------------------
# Alerts section
# ---------------------------------------------------------------------------


def test_dry_run_mode_warns():
    result = _build(_status(), dry_run=True)
    assert result["mode"]["state"] == "warn"
    assert result["mode"]["value"] == "dry run"


def test_empty_notifiers_warns():
    result = _build(_status(), notifiers=[])
    assert result["notifiers"]["state"] == "warn"


def test_delivery_failures_warn_when_nonzero():
    result = _build(_status(finding={"delivery_status_row_counts_today": {"failed": 1}}))
    assert result["delivery_failures_today"]["state"] == "warn"


def test_alerts_today_is_info_with_live_and_dry_split():
    result = _build(
        _status(finding={"alert_events_today_live": 4, "alert_events_today_dry": 2})
    )
    assert result["alerts_today"]["state"] == "info"
    assert result["alerts_today"]["value"] == "4 live / 2 dry"


def test_budget_period_value_includes_the_date():
    # V0.11a's C3 amendment - "current" alone means nothing; the date is
    # what makes it mean something.
    result = _build(_status())
    assert result["budget_period"]["value"] == "current (2026-09-12)"


def test_budget_period_stale_value_includes_the_date_too():
    status = _status()
    status["alive"]["budget"]["period"] = "2026-09-10"
    status["alive"]["budget"]["period_is_today"] = False
    result = _build(status)
    assert result["budget_period"]["value"] == "stale (2026-09-10)"


# ---------------------------------------------------------------------------
# build_budget_pacing (V0.11a Part C2)
# ---------------------------------------------------------------------------


def test_budget_pacing_consumed_fraction():
    status = _status()
    status["alive"]["budget"]["used"] = 950
    status["alive"]["budget"]["ceiling"] = 4750

    pacing = build_budget_pacing(status, now=1_000_000)

    assert pacing["budget_pct"] == 20.0
    assert pacing["budget_pct_display"] == "20%"


def test_budget_pacing_consumed_is_unknown_when_ceiling_is_none():
    status = _status()
    status["alive"]["budget"]["ceiling"] = None

    pacing = build_budget_pacing(status, now=1_000_000)

    assert pacing["budget_pct"] is None
    assert pacing["budget_pct_display"] == "unknown"


def test_budget_pacing_consumed_is_clamped_at_100_when_used_exceeds_ceiling():
    status = _status()
    status["alive"]["budget"]["used"] = 6000
    status["alive"]["budget"]["ceiling"] = 4750

    pacing = build_budget_pacing(status, now=1_000_000)

    assert pacing["budget_pct"] == 100.0


def test_budget_pacing_day_fraction_reflects_pt_not_utc():
    # 2026-09-13 02:00 UTC = 2026-09-12 19:00 PDT - UTC and PT are on
    # DIFFERENT calendar dates here. Correctly computed from
    # la_day_bounds() (PT midnight to PT midnight), 19 of 24 PT hours
    # have elapsed: ~79%. Computed from a UTC midnight boundary instead,
    # only 2 of 24 UTC hours have elapsed: ~8% - a completely different,
    # easily distinguishable number.
    now = int(datetime(2026, 9, 13, 2, 0, tzinfo=timezone.utc).timestamp())
    status = _status()

    pacing = build_budget_pacing(status, now)

    assert pacing["day_pct"] == pytest.approx(79.2, abs=0.1)
