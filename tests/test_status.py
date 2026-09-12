"""Tests for dealwatch.reporting.status.collect_status() (V0.10,
design.md §12).

Real SQLite under tmp_path, built with the real connect() so migrations
run, seeded with hand-written INSERTs against listings/observations/
sweeps/alerts/baselines/budget directly - collect_status() is a pure read
layer over whatever's already in those tables, not over the
collector/scoring pipeline that normally produces them, so there is no
reason to run listings through record_sighting()/normalize() here.
"""

import sqlite3
import time
from datetime import datetime, timedelta

from dealwatch.providers.ratelimit import PACIFIC, _today_la
from dealwatch.reporting.status import collect_status
from dealwatch.storage.sqlite import connect

PROFILE = "thinkpad-t14"
OTHER_PROFILE = "other-profile"


def make_conn(tmp_path):
    return connect(tmp_path / "dealwatch.db")


def seed_listing(
    conn,
    item_id,
    *,
    profile_id=PROFILE,
    title="t",
    spec_status="ok",
    bucket_key="1|intel-10th|16",
    first_seen=1000,
    last_seen=1000,
    gone_at=None,
    lifespan_mins=None,
):
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "bucket_key, first_seen, last_seen, miss_count, gone_at, lifespan_mins) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        (item_id, profile_id, title, spec_status, bucket_key, first_seen,
         last_seen, gone_at, lifespan_mins),
    )


def seed_observation(conn, item_id, observed_at, *, price_cents=10000):
    conn.execute(
        "INSERT INTO observations (item_id, observed_at, price_cents, raw_json) "
        "VALUES (?, ?, ?, '{}')",
        (item_id, observed_at, price_cents),
    )


def seed_sweep(
    conn,
    *,
    profile_id=PROFILE,
    swept_at,
    fetched_count=100,
    distinct_count=100,
    active_count_before=100,
    truncated=False,
    sweep_recorded=True,
):
    conn.execute(
        "INSERT INTO sweeps (profile_id, swept_at, fetched_count, distinct_count, "
        "active_count_before, truncated, sweep_recorded) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (profile_id, swept_at, fetched_count, distinct_count, active_count_before,
         1 if truncated else 0, 1 if sweep_recorded else 0),
    )


def seed_alert(
    conn,
    item_id,
    *,
    profile_id=PROFILE,
    sent_at,
    dry_run=False,
    notifier="discord",
    delivery_status="sent",
    ratio_to_p25=0.9,
    baseline_layer="seed",
):
    conn.execute(
        "INSERT INTO alerts (item_id, profile_id, sent_at, dry_run, price_cents, "
        "price_is_price_only, bucket_key, baseline_layer, baseline_match, baseline_n, "
        "baseline_p25_cents, baseline_p50_cents, ratio_to_p25, sanity_flagged, "
        "delivery_status, notifier) VALUES (?, ?, ?, ?, 9000, 0, '1|intel-10th|16', "
        "?, '{}', NULL, 10000, 15000, ?, 0, ?, ?)",
        (item_id, profile_id, sent_at, 1 if dry_run else 0, baseline_layer,
         ratio_to_p25, delivery_status, notifier),
    )


def seed_baseline(conn, *, profile_id=PROFILE, bucket_key="1|intel-10th|16", computed_at=1000):
    conn.execute(
        "INSERT INTO baselines (profile_id, bucket_key, n, n_price_only, p10_cents, "
        "p25_cents, p50_cents, fast_hours, computed_at) VALUES (?, ?, 12, 0, 9000, "
        "10000, 15000, 24, ?)",
        (profile_id, bucket_key, computed_at),
    )


def seed_budget(conn, *, period, used):
    conn.execute("INSERT OR REPLACE INTO budget (id, period, used) VALUES (1, ?, ?)", (period, used))


# A fixed "now" at LA noon, safely inside a DST-stable day, used by tests
# that don't care about the exact date - avoids every test having to pick
# its own timestamp.
_NOON = int(datetime(2026, 9, 10, 12, 0, tzinfo=PACIFIC).timestamp())
_TODAY_START = int(datetime(2026, 9, 10, 0, 0, tzinfo=PACIFIC).timestamp())


# ---------------------------------------------------------------------------
# 1. Sweep attempts vs recorded
# ---------------------------------------------------------------------------


def test_sweep_attempts_vs_recorded(tmp_path):
    # Breaking mechanism: sweeps_today_recorded counting ALL rows instead
    # of filtering sweep_recorded=1 would make total == recorded here.
    conn = make_conn(tmp_path)
    seed_sweep(conn, swept_at=_TODAY_START + 100, sweep_recorded=True)
    seed_sweep(conn, swept_at=_TODAY_START + 200, sweep_recorded=False)

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["alive"]["sweeps_today_total"] == 2
    assert status["alive"]["sweeps_today_recorded"] == 1


# ---------------------------------------------------------------------------
# 2. Coverage picks the right row
# ---------------------------------------------------------------------------


def test_coverage_picks_the_latest_recorded_row_not_the_latest_overall(tmp_path):
    # Breaking mechanism: querying the latest sweeps row overall (ORDER BY
    # swept_at DESC LIMIT 1, no sweep_recorded filter) would pick the later
    # unrecorded row and report 0% coverage from its distinct_count=0.
    conn = make_conn(tmp_path)
    seed_sweep(
        conn, swept_at=_TODAY_START + 100, sweep_recorded=True,
        distinct_count=50, active_count_before=100, fetched_count=55,
    )
    seed_sweep(
        conn, swept_at=_TODAY_START + 200, sweep_recorded=False,
        distinct_count=0, active_count_before=100, fetched_count=0,
    )

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["collecting"]["last_sweep_coverage_pct"] == 0.5
    assert status["collecting"]["last_sweep_page_drift"] == 5


# ---------------------------------------------------------------------------
# 3. Multi-notifier events
# ---------------------------------------------------------------------------


def test_multi_notifier_events_count_once_rows_count_per_notifier(tmp_path):
    # Breaking mechanism: a bare COUNT(*) instead of the DISTINCT
    # (item_id, sent_at) subquery would report alert_events_today_live=2.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1")
    seed_alert(conn, "item-1", sent_at=_TODAY_START + 100, notifier="discord")
    seed_alert(conn, "item-1", sent_at=_TODAY_START + 100, notifier="pushover")

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["finding"]["alert_events_today_live"] == 1
    assert status["finding"]["alert_rows_today"] == 2


# ---------------------------------------------------------------------------
# 4. Unmeasured deaths
# ---------------------------------------------------------------------------


def test_unmeasured_deaths_counts_only_null_lifespan(tmp_path):
    # Breaking mechanism: using first_seen == last_seen instead of
    # lifespan_mins IS NULL, or failing to distinguish NULL from 0, would
    # either miscount or count the real-measurement row too.
    conn = make_conn(tmp_path)
    seed_listing(conn, "never-swept", gone_at=_TODAY_START + 100, lifespan_mins=None)
    seed_listing(conn, "swept-fast", gone_at=_TODAY_START + 200, lifespan_mins=0)

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["collecting"]["deaths_today"] == 2
    assert status["collecting"]["deaths_today_unmeasured"] == 1


# ---------------------------------------------------------------------------
# 5. Profile isolation
# ---------------------------------------------------------------------------


def test_profile_isolation(tmp_path):
    # Breaking mechanism: dropping a WHERE profile_id = ? filter anywhere
    # would leak the other profile's rows into these counts.
    conn = make_conn(tmp_path)
    seed_listing(conn, "mine", profile_id=PROFILE, gone_at=_TODAY_START + 50, lifespan_mins=5)
    seed_listing(conn, "theirs", profile_id=OTHER_PROFILE)
    seed_observation(conn, "theirs", _TODAY_START + 60)
    seed_sweep(conn, profile_id=OTHER_PROFILE, swept_at=_TODAY_START + 70)
    seed_alert(conn, "theirs", profile_id=OTHER_PROFILE, sent_at=_TODAY_START + 80)
    seed_baseline(conn, profile_id=OTHER_PROFILE)

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["collecting"]["active_listings"] == 0  # "mine" is dead, "theirs" isn't ours
    assert status["collecting"]["deaths_today"] == 1  # only "mine"
    assert status["alive"]["sweeps_today_total"] == 0
    assert status["alive"]["last_price_change_at"] is None
    assert status["finding"]["alert_rows_today"] == 0
    assert status["baseline"]["baseline_buckets_total"] == 0


# ---------------------------------------------------------------------------
# 6. Day boundary
# ---------------------------------------------------------------------------


def test_day_boundary_excludes_yesterday(tmp_path):
    # Breaking mechanism: an off-by-one on day_start/day_end (e.g. using
    # <= instead of <, or the wrong boundary entirely) would either
    # include yesterday's row or exclude today's.
    conn = make_conn(tmp_path)
    yesterday_end = _TODAY_START - 1
    seed_sweep(conn, swept_at=yesterday_end)  # one second before today
    seed_sweep(conn, swept_at=_TODAY_START)  # exactly at today's start

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["alive"]["sweeps_today_total"] == 1


def test_day_boundary_across_a_dst_transition(tmp_path):
    # 2026-11-01: US DST ends - this LA calendar day is 25 hours long. A
    # fixed 86400-second window would exclude a sweep that happened during
    # the extra hour but is still within the correct calendar day.
    conn = make_conn(tmp_path)
    dst_day_start = int(datetime(2026, 11, 1, 0, 0, tzinfo=PACIFIC).timestamp())
    dst_noon = int(datetime(2026, 11, 1, 12, 0, tzinfo=PACIFIC).timestamp())
    late_in_the_25_hour_day = dst_day_start + 24 * 3600 + 60  # in the extra hour
    seed_sweep(conn, swept_at=late_in_the_25_hour_day)

    status = collect_status(conn, PROFILE, now=dst_noon)

    assert status["alive"]["sweeps_today_total"] == 1


# ---------------------------------------------------------------------------
# 7. la_day_bounds agrees with _today_la()
# ---------------------------------------------------------------------------


def test_la_day_bounds_agrees_with_today_la_via_collect_status():
    # Same invariant tests/test_ratelimit.py checks directly against
    # la_day_bounds() - repeated here because it's the thing that would
    # silently give this module a different "today" than the budget uses.
    now = int(time.time())
    from dealwatch.providers.ratelimit import la_day_bounds

    day_start, _ = la_day_bounds(now)
    assert datetime.fromtimestamp(day_start, PACIFIC).date().isoformat() == _today_la()


# ---------------------------------------------------------------------------
# 8. Empty database
# ---------------------------------------------------------------------------


def test_empty_database_returns_zero_or_none_never_raises(tmp_path):
    # The budget table specifically starts empty on a freshly migrated
    # database - no migration inserts a row, only DailyBudget._ensure_row()
    # does, and that never runs here. A database that has never had the
    # collector started against it (this one) has no budget row at all,
    # not a zeroed one.
    conn = make_conn(tmp_path)

    status = collect_status(conn, PROFILE, ceiling=4750, now=_NOON)

    assert status["alive"]["last_sweep_started_at"] is None
    assert status["collecting"]["active_listings"] == 0
    assert status["collecting"]["last_sweep_coverage_pct"] is None  # not 0.0, no ZeroDivisionError
    assert status["finding"]["best_ratio_24h"] is None
    assert status["baseline"]["baseline_buckets_total"] == 0
    budget = status["alive"]["budget"]
    assert budget["period"] is None
    assert budget["used"] is None
    assert budget["period_is_today"] is None
    assert budget["ceiling"] == 4750  # ceiling comes from the caller, unaffected by no row
    assert budget["remaining"] is None  # not 4750, not 0 - used is unknown, so remaining is too


# ---------------------------------------------------------------------------
# 9. row_factory independence
# ---------------------------------------------------------------------------


def test_collect_status_is_identical_regardless_of_row_factory(tmp_path):
    # Breaking mechanism: a single row["column_name"] access anywhere in
    # status.py would raise (plain tuples don't support string indexing)
    # or silently misbehave against the plain-tuple connection below.
    # Both of today's ACTUAL callers (storage.sqlite.connect() and
    # scripts/'s open_readonly()) set row_factory = sqlite3.Row, so this
    # pins a contract for consumers that don't exist yet - it does not
    # catch a currently-live bug, and the test says so rather than
    # pretending otherwise.
    # Every table this module reads from must be non-empty here - a query
    # against an empty table never reaches the row-access code at all, so
    # an empty table is silently untested for this specific trap (this is
    # exactly how the first version of this test missed a real sabotage:
    # it had no budget row, so the budget branch's name-indexed access
    # went unexercised). listings/observations/alerts/baselines/budget
    # each get at least one row; sweeps gets one of EACH sweep_recorded
    # value, since that column drives a real branch in _alive/_collecting.
    db_path = tmp_path / "dealwatch.db"
    conn_with_row = connect(db_path)  # sets row_factory = sqlite3.Row
    seed_listing(conn_with_row, "item-1", gone_at=_TODAY_START + 10, lifespan_mins=None)
    seed_observation(conn_with_row, "item-1", _TODAY_START + 15)
    seed_sweep(conn_with_row, swept_at=_TODAY_START + 20, sweep_recorded=True)
    seed_sweep(conn_with_row, swept_at=_TODAY_START + 25, sweep_recorded=False)
    seed_alert(conn_with_row, "item-1", sent_at=_TODAY_START + 30)
    seed_baseline(conn_with_row)
    seed_budget(conn_with_row, period=_today_la(), used=5)

    status_with_row_factory = collect_status(conn_with_row, PROFILE, ceiling=4750, now=_NOON)

    conn_plain = sqlite3.connect(str(db_path))  # row_factory left unset - plain tuples
    status_plain = collect_status(conn_plain, PROFILE, ceiling=4750, now=_NOON)
    conn_plain.close()

    assert status_with_row_factory == status_plain


# ---------------------------------------------------------------------------
# 10. Budget stale period
# ---------------------------------------------------------------------------


def test_budget_stale_period_is_reported_not_rolled_over(tmp_path):
    # Breaking mechanism: calling DailyBudget.status() (which rolls the
    # period over and zeroes `used`) instead of reading the budget table
    # directly would report period_is_today=True and used=0.
    conn = make_conn(tmp_path)
    yesterday = (datetime.fromtimestamp(_TODAY_START, PACIFIC) - timedelta(days=1)).date().isoformat()
    seed_budget(conn, period=yesterday, used=17)

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["alive"]["budget"]["period"] == yesterday
    assert status["alive"]["budget"]["used"] == 17
    assert status["alive"]["budget"]["period_is_today"] is False


# ---------------------------------------------------------------------------
# 11. ceiling=None
# ---------------------------------------------------------------------------


def test_ceiling_none_leaves_ceiling_and_remaining_none(tmp_path):
    conn = make_conn(tmp_path)
    seed_budget(conn, period=_today_la(), used=10)

    status = collect_status(conn, PROFILE, ceiling=None, now=_NOON)

    assert status["alive"]["budget"]["ceiling"] is None
    assert status["alive"]["budget"]["remaining"] is None
    assert status["alive"]["budget"]["used"] == 10  # used is still reported


# ---------------------------------------------------------------------------
# Extra: sweep_bookkeeping_consistent's None-vs-False distinction
# ---------------------------------------------------------------------------


def test_sweep_bookkeeping_consistent_is_none_when_no_recorded_sweep_exists(tmp_path):
    conn = make_conn(tmp_path)
    status = collect_status(conn, PROFILE, now=_NOON)
    assert status["alive"]["sweep_bookkeeping_consistent"] is None


def test_sweep_bookkeeping_consistent_is_none_not_false_on_a_zero_item_recorded_sweep(tmp_path):
    # Breaking mechanism: comparing listings_last_seen_max (None here,
    # since no listing was ever seen) against last_sweep_started_at
    # without the distinct_count=0 special case would report False - a
    # sweep that legitimately returned nothing looks like broken
    # bookkeeping instead of an unevaluable invariant.
    conn = make_conn(tmp_path)
    seed_sweep(conn, swept_at=_TODAY_START + 10, distinct_count=0, sweep_recorded=True)

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["alive"]["sweep_bookkeeping_consistent"] is None


def test_sweep_bookkeeping_consistent_true_when_they_agree(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", last_seen=_TODAY_START + 10)
    seed_sweep(conn, swept_at=_TODAY_START + 10, distinct_count=1)

    status = collect_status(conn, PROFILE, now=_NOON)

    assert status["alive"]["sweep_bookkeeping_consistent"] is True
