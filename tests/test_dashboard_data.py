"""Tests for dealwatch.reporting.dashboard_data (V0.11/V0.11a, design.md
§13): build_payload()'s per-section isolation, get_payload()'s TTL cache,
and the V0.11a database-unavailable banner.
"""

import sqlite3

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
        daily_call_limit=5000,
        daily_reserve_calls=250,
        min_samples=12,
        fast_lifespan_hours=24,
        compiled_seeds=[],
    )
    base.update(overrides)
    return base


_ALL_SECTIONS = (
    "status", "indicators", "budget_pacing", "alerts_per_day", "alerts_summary",
    "recent_alerts", "recent_listings", "baseline_coverage", "computed_baselines",
    "baseline_queue",
)


# ---------------------------------------------------------------------------
# build_payload - assembly and per-section isolation
# ---------------------------------------------------------------------------


def test_build_payload_assembles_every_section(tmp_path):
    conn = make_conn(tmp_path)

    payload = dashboard_data.build_payload(conn, now=1_000_000, **_kwargs())

    assert payload["generated_at"] == 1_000_000
    assert payload["profile_id"] == PROFILE
    assert payload["database_error"] is None
    assert payload["budget_ceiling_display"] == "4750 usable (5000 − 250 reserved)"
    for key in _ALL_SECTIONS:
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
    assert "error" not in payload["budget_pacing"]
    assert "error" not in payload["recent_listings"]
    assert "error" not in payload["baseline_coverage"]
    assert "error" not in payload["computed_baselines"]
    assert "error" not in payload["baseline_queue"]
    assert isinstance(payload["alerts_per_day"], list)


def test_indicators_and_pacing_both_carry_an_error_when_status_itself_fails(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated status failure")

    monkeypatch.setattr(dashboard_data, "collect_status", _boom)

    payload = dashboard_data.build_payload(conn, now=1_000_000, **_kwargs())

    assert "error" in payload["status"]
    assert "error" in payload["indicators"]
    assert "error" in payload["budget_pacing"]
    # Panel sections don't depend on status - they still build.
    assert "error" not in payload["recent_listings"]
    assert "error" not in payload["computed_baselines"]


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


# ---------------------------------------------------------------------------
# V0.11a Part A - the database-unavailable banner
# ---------------------------------------------------------------------------


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
    assert payload["profile_id"] == PROFILE
    assert "unable to open database file" in payload["database_error"]
    # ONE signal, not six per-section errors (V0.11a's Part A rewrite).
    for key in _ALL_SECTIONS:
        assert payload[key] is None, key
    # Still derivable with no query at all - the header/budget-ceiling
    # note render even with the database gone.
    assert payload["budget_ceiling_display"] == "4750 usable (5000 − 250 reserved)"


def test_database_unavailable_logs_one_warning_line_with_no_traceback(tmp_path, caplog):
    # V0.11b Part B2: this is a handled, expected condition whose message
    # is already in the payload and rendered on the page - a full
    # traceback here would repeat every _DATABASE_ERROR_TTL_SECONDS, per
    # open tab, for as long as the database stays unavailable. Must be
    # logger.warning(), not logger.exception() - caplog records carry
    # exc_info only for the latter.
    import logging

    with caplog.at_level(logging.INFO, logger="dealwatch.reporting.dashboard_data"):
        dashboard_data.get_payload(
            tmp_path / "never-created.db", ttl_seconds=30, now=1_000_000, **_kwargs()
        )

    warning_records = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warning_records) == 1
    assert warning_records[0].exc_info is None


def test_database_error_is_none_on_a_healthy_payload(tmp_path):
    conn = make_conn(tmp_path)
    payload = dashboard_data.build_payload(conn, now=1_000_000, **_kwargs())
    assert payload["database_error"] is None


def test_only_database_error_is_treated_as_database_unavailable(tmp_path, monkeypatch):
    # The except in get_payload() must be narrow - a blanket `except
    # Exception` around connect_readonly()/build_payload() would mislabel
    # an unrelated bug (a real TypeError, say) as "database unavailable"
    # instead of letting it surface as the actual error it is. Widened to
    # sqlite3.DatabaseError in V0.11b (OperationalError's own parent class,
    # a strict superset of the old catch) - TypeError is neither, so this
    # must still propagate uncaught either way.
    def _boom(*args, **kwargs):
        raise TypeError("not a database problem at all")

    monkeypatch.setattr(dashboard_data, "connect_readonly", _boom)

    try:
        dashboard_data.get_payload(tmp_path / "whatever.db", ttl_seconds=30, **_kwargs())
    except TypeError as exc:
        assert "not a database problem at all" in str(exc)
    else:
        raise AssertionError("expected the unrelated TypeError to propagate, not be swallowed")


def test_a_corrupted_database_file_renders_the_banner_not_a_crash(tmp_path):
    # V0.11b Part C: the realistic version of "database unavailable" - a
    # botched snapshot restore (README's documented procedure) leaves a
    # truncated or wrong file at the db path. connect_readonly() itself
    # succeeds (a mode=ro connection is lazy - it doesn't read the file
    # header until the first real query), so the old
    # sqlite3.OperationalError-only catch around JUST connect_readonly()
    # missed this case entirely: the failure surfaced deep inside
    # build_payload()'s first _safe()-wrapped query instead, which used
    # to swallow it into a per-section {"error": ...} - six identical
    # "Panel unavailable" boxes, not the one banner Part D wants for a
    # whole-database problem. get_payload() must render the single banner
    # and return a payload, not propagate sqlite3.DatabaseError.
    db_path = tmp_path / "corrupted.db"
    db_path.write_bytes(b"not a sqlite database at all, just garbage bytes 1234567890")

    payload = dashboard_data.get_payload(db_path, ttl_seconds=30, now=1_000_000, **_kwargs())

    assert payload["database_error"] is not None
    assert "not a database" in payload["database_error"]
    for key in _ALL_SECTIONS:
        assert payload[key] is None, key


def test_database_unavailable_state_is_not_cached_for_the_full_page_ttl(tmp_path, monkeypatch):
    # A transient failure that recovers mid-render must not read as
    # "still broken" for a stale 30s afterward - design.md §13's V0.11a
    # Part A. Uses the real 30s page ttl_seconds but simulates recovery
    # after the shorter database-error TTL elapses.
    db_path = tmp_path / "dealwatch.db"

    fake_time = [1000.0]
    monkeypatch.setattr(dashboard_data.time, "monotonic", lambda: fake_time[0])
    monkeypatch.setattr(dashboard_data.time, "time", lambda: fake_time[0])

    # First call: database genuinely doesn't exist yet.
    first = dashboard_data.get_payload(db_path, ttl_seconds=30, **_kwargs())
    assert first["database_error"] is not None

    # The database becomes available a couple of seconds later - well
    # inside the normal 30s page TTL, but past the shorter error TTL.
    writer = connect(db_path)
    try:
        fake_time[0] += dashboard_data._DATABASE_ERROR_TTL_SECONDS + 1

        second = dashboard_data.get_payload(db_path, ttl_seconds=30, **_kwargs())

        assert second["database_error"] is None
        assert "error" not in second["status"]
    finally:
        writer.close()
