"""Tests for dealwatch.reporting.dashboard_data (V0.11, design.md §13):
build_payload()'s per-section isolation and get_payload()'s TTL cache.
"""

import time

from dealwatch.reporting import dashboard_data, panels
from dealwatch.reporting.status import collect_status
from dealwatch.storage.sqlite import connect

PROFILE = "thinkpad-t14"


def make_conn(tmp_path):
    return connect(tmp_path / "dealwatch.db")


def _kwargs(**overrides):
    base = dict(
        profile_id=PROFILE,
        sweep_interval_minutes=60,
        dry_run=False,
        notifiers=["discord"],
        ceiling=4750,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# build_payload - assembly and per-section isolation
# ---------------------------------------------------------------------------


def test_build_payload_assembles_every_section(tmp_path):
    conn = make_conn(tmp_path)

    payload = dashboard_data.build_payload(conn, now=1_000_000, **_kwargs())

    assert payload["generated_at"] == 1_000_000
    for key in (
        "status", "indicators", "alerts_per_day", "recent_alerts",
        "recent_listings", "baseline_coverage",
    ):
        assert key in payload
        assert "error" not in payload[key] if isinstance(payload[key], dict) else True


def test_a_failing_panel_does_not_take_down_the_rest_of_the_payload(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated query failure")

    monkeypatch.setattr(panels, "recent_alerts", _boom)

    payload = dashboard_data.build_payload(conn, now=1_000_000, **_kwargs())

    assert "error" in payload["recent_alerts"]
    assert "RuntimeError" in payload["recent_alerts"]["error"]
    # Every other section still built normally.
    assert "error" not in payload["status"]
    assert "error" not in payload["indicators"]
    assert "error" not in payload["recent_listings"]
    assert "error" not in payload["baseline_coverage"]
    assert isinstance(payload["alerts_per_day"], list)


def test_indicators_carries_an_error_when_status_itself_fails(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated status failure")

    monkeypatch.setattr(dashboard_data, "collect_status", _boom)

    payload = dashboard_data.build_payload(conn, now=1_000_000, **_kwargs())

    assert "error" in payload["status"]
    assert "error" in payload["indicators"]
    # Panel sections don't depend on status - they still build.
    assert "error" not in payload["recent_listings"]


# ---------------------------------------------------------------------------
# get_payload - TTL cache
# ---------------------------------------------------------------------------


def test_cache_hit_returns_identical_generated_at(tmp_path):
    db_path = tmp_path / "dealwatch.db"
    writer = connect(db_path)  # keeps WAL sidecars alive for connect_readonly()
    try:
        first = dashboard_data.get_payload(db_path, ttl_seconds=60, **_kwargs())
        second = dashboard_data.get_payload(db_path, ttl_seconds=60, **_kwargs())
        assert first["generated_at"] == second["generated_at"]
        assert first == second
    finally:
        writer.close()


def test_cache_expires_after_ttl(tmp_path, monkeypatch):
    db_path = tmp_path / "dealwatch.db"
    writer = connect(db_path)
    try:
        # Fake both clocks together: monotonic() gates the TTL, time()
        # (via build_payload's `now` default) is what generated_at
        # actually records - real wall-clock time barely moves between
        # two calls in the same test, so without faking time() too, a
        # cache MISS on the second call could still coincidentally
        # compute the same generated_at and mask a real caching bug.
        fake_time = [1000.0]
        monkeypatch.setattr(dashboard_data.time, "monotonic", lambda: fake_time[0])
        monkeypatch.setattr(dashboard_data.time, "time", lambda: fake_time[0])

        first = dashboard_data.get_payload(db_path, ttl_seconds=30, **_kwargs())
        fake_time[0] += 31  # past the TTL
        second = dashboard_data.get_payload(db_path, ttl_seconds=30, **_kwargs())

        assert first["generated_at"] != second["generated_at"]
    finally:
        writer.close()


def test_cache_treats_a_kwargs_change_as_a_miss(tmp_path):
    db_path = tmp_path / "dealwatch.db"
    writer = connect(db_path)
    try:
        first = dashboard_data.get_payload(db_path, ttl_seconds=60, **_kwargs(dry_run=False))
        second = dashboard_data.get_payload(db_path, ttl_seconds=60, **_kwargs(dry_run=True))

        assert first["indicators"]["mode"]["state"] == "ok"
        assert second["indicators"]["mode"]["state"] == "warn"
    finally:
        writer.close()


def test_get_payload_survives_a_database_that_does_not_exist_yet(tmp_path):
    # Found via live Docker verification, not theorized: on a container
    # that has never had a writer create data/dealwatch.db (no collector
    # started, /health never hit either - exactly the credentials-missing
    # scenario B1 is about), connect_readonly() itself raises before
    # build_payload() ever runs, which is a failure mode outside
    # build_payload()'s own per-section try/except. get_payload() must
    # still return a renderable payload, not propagate the exception.
    missing_db_path = tmp_path / "never-created.db"

    payload = dashboard_data.get_payload(missing_db_path, ttl_seconds=60, now=1_000_000, **_kwargs())

    assert payload["generated_at"] == 1_000_000
    for key in (
        "status", "indicators", "alerts_per_day", "recent_alerts",
        "recent_listings", "baseline_coverage",
    ):
        assert "error" in payload[key], key
        assert "unable to open database file" in payload[key]["error"]
