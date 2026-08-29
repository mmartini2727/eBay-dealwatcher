"""Tests for DailyBudget (dealwatch.providers.ratelimit).

Every test uses a real SQLite file under tmp_path - there's no mocking the
persistence layer here, since persistence-across-restart is the entire
point of this milestone. No network, no live credentials.
"""

import threading
from datetime import date, datetime, timedelta

from dealwatch.config import Settings
from dealwatch.providers.ratelimit import PACIFIC, DailyBudget, _today_la
from dealwatch.storage.sqlite import connect, default_db_path


def make_settings(tmp_path, *, daily_call_limit=5000, daily_reserve_calls=250):
    return Settings(
        db_path=str(tmp_path / "dealwatch.db"),
        daily_call_limit=daily_call_limit,
        daily_reserve_calls=daily_reserve_calls,
    )


def test_reserve_decrements_remaining(tmp_path):
    settings = make_settings(tmp_path, daily_call_limit=100, daily_reserve_calls=10)
    budget = DailyBudget(settings)

    assert budget.reserve() is True
    status = budget.status()
    assert status["used"] == 1
    assert status["ceiling"] == 90
    assert status["remaining"] == 89


def test_hard_stop_fires_at_limit_minus_reserve_not_at_limit(tmp_path):
    # ceiling = 100 - 10 = 90: the 90th reservation must succeed, the 91st
    # must not - the hard stop is at the ceiling, not at daily_call_limit.
    settings = make_settings(tmp_path, daily_call_limit=100, daily_reserve_calls=10)
    budget = DailyBudget(settings)

    for _ in range(90):
        assert budget.reserve() is True

    assert budget.status()["used"] == 90
    assert budget.reserve() is False
    assert budget.status()["used"] == 90  # refused reservation didn't get counted


def test_restart_sees_prior_count(tmp_path):
    settings = make_settings(tmp_path, daily_call_limit=100, daily_reserve_calls=10)

    first = DailyBudget(settings)
    for _ in range(5):
        assert first.reserve() is True

    # A fresh instance against the same file - simulating a container
    # restart - must see the 5 calls already spent today, not start at 0.
    second = DailyBudget(settings)
    assert second.status()["used"] == 5
    assert second.reserve() is True
    assert second.status()["used"] == 6


def test_restart_across_day_boundary_rolls_over(tmp_path):
    # The real production restart path: the on-disk row is whatever
    # yesterday left behind, and nothing runs at midnight to clean it up -
    # the first status()/reserve() call after restart has to notice and
    # roll over. Seed that state directly rather than via DailyBudget, since
    # DailyBudget itself never writes a stale row on purpose.
    settings = make_settings(tmp_path, daily_call_limit=100, daily_reserve_calls=10)
    today = _today_la()
    yesterday = (date.fromisoformat(today) - timedelta(days=1)).isoformat()

    conn = connect(default_db_path(settings))
    conn.execute(
        "INSERT OR REPLACE INTO budget (id, period, used) VALUES (1, ?, ?)",
        (yesterday, 42),
    )
    conn.close()

    budget = DailyBudget(settings)
    status = budget.status()
    assert status["period"] == today
    assert status["used"] == 0


def test_concurrent_reservations_at_ceiling_do_not_oversubscribe(tmp_path):
    ceiling = 20
    settings = make_settings(tmp_path, daily_call_limit=ceiling, daily_reserve_calls=0)

    successes = []
    lock = threading.Lock()
    barrier = threading.Barrier(50)

    def worker():
        # Each thread opens its own DailyBudget/connection against the same
        # file - the scenario this protects against is separate processes
        # (or threads) racing on the same SQLite file, not sharing one
        # Python-level lock. The barrier makes sure all 50 actually hit
        # reserve() together instead of being staggered by connection setup.
        budget = DailyBudget(settings)
        barrier.wait()
        ok = budget.reserve()
        with lock:
            successes.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert successes.count(True) == ceiling

    checker = DailyBudget(settings)
    assert checker.status()["used"] == ceiling


def test_concurrent_reservations_shared_instance_do_not_oversubscribe(tmp_path):
    # The production shape: one DailyBudget, lru_cache'd as a FastAPI
    # dependency, called from uvicorn's threadpool and from
    # asyncio.to_thread in EbayBrowseProvider - i.e. many threads sharing a
    # single instance, not one instance per thread. This is only safe
    # because reserve()/status() open and close their own connection per
    # call rather than holding one across the instance's lifetime.
    ceiling = 20
    settings = make_settings(tmp_path, daily_call_limit=ceiling, daily_reserve_calls=0)
    budget = DailyBudget(settings)

    successes = []
    lock = threading.Lock()
    barrier = threading.Barrier(50)

    def worker():
        barrier.wait()
        ok = budget.reserve()
        with lock:
            successes.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(50)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert successes.count(True) == ceiling
    assert budget.status()["used"] == ceiling


def test_la_date_rollover_resets_counter(tmp_path, monkeypatch):
    settings = make_settings(tmp_path, daily_call_limit=100, daily_reserve_calls=10)
    budget = DailyBudget(settings)

    assert budget.reserve() is True
    assert budget.status()["used"] == 1

    # Simulate "a day has passed in LA" by making _today_la report tomorrow
    # relative to LA's current date, not the test runner's local date -
    # deriving from date.today() would be wrong (and occasionally flaky)
    # for anyone not running tests in the Pacific timezone.
    today = _today_la()
    tomorrow = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
    monkeypatch.setattr(
        "dealwatch.providers.ratelimit._today_la", lambda: tomorrow
    )

    status = budget.status()
    assert status["period"] == tomorrow
    assert status["used"] == 0

    assert budget.reserve() is True
    assert budget.status()["used"] == 1


def test_pacific_has_dst_not_a_fixed_offset():
    # A hardcoded UTC-8 offset would pass a test that merely checks the
    # zone's name string - it has to actually observe two different offsets
    # across the DST boundary to prove it's a real zoneinfo, not a fixed
    # offset with "America/Los_Angeles" as a label. Also fails loudly (with
    # ZoneInfoNotFoundError) if the tzdata package is missing, which a
    # name-string check would not catch either.
    january = datetime(2026, 1, 15, tzinfo=PACIFIC).utcoffset()
    july = datetime(2026, 7, 15, tzinfo=PACIFIC).utcoffset()
    assert january != july
