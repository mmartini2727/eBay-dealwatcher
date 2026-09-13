"""Tests for GET / (V0.11's dashboard route) and GET /health (unchanged),
via FastAPI's TestClient (design.md §13, Part D).

Settings and DailyBudget are both @lru_cache'd module-level singletons in
main.py - env vars set via monkeypatch only take effect once their caches
are cleared, so every test clears both before constructing a client.
dealwatch.reporting.dashboard_data's own TTL cache is keyed by db_path,
which is a fresh tmp_path per test, so it needs no explicit clearing.
"""

import inspect
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import dealwatch.main as main_module
from dealwatch.config import get_settings
from dealwatch.storage.sqlite import connect

# Absolute, not "profiles/thinkpad-t14.yaml" - a few tests below
# deliberately change the process cwd to prove main.py's template
# resolution is cwd-independent, and profile_path is a SEPARATE,
# already-accepted repo-relative convention (Settings.profile_path) that
# these tests must not also break as a side effect of testing something
# else.
_REAL_PROFILE_PATH = str(Path(__file__).resolve().parent.parent / "profiles" / "thinkpad-t14.yaml")


@pytest.fixture(autouse=True)
def _clear_caches():
    get_settings.cache_clear()
    main_module.get_budget.cache_clear()
    yield
    get_settings.cache_clear()
    main_module.get_budget.cache_clear()


def _make_client(
    tmp_path, monkeypatch, *, seed=True, profile_path=_REAL_PROFILE_PATH, create_db=True
):
    db_path = tmp_path / "dealwatch.db"
    if create_db:
        conn = connect(db_path)
        if seed:
            conn.execute(
                "INSERT INTO listings (item_id, profile_id, title, spec_status, "
                "bucket_key, first_seen, last_seen, miss_count, gone_at, lifespan_mins, "
                "item_web_url) VALUES ('item-1', 'thinkpad-t14', 'Test listing', 'ok', "
                "'1|intel-10th|16', 1000, 1000, 0, NULL, NULL, 'https://example.com/1')"
            )
        conn.close()
    # create_db=False deliberately leaves nothing at db_path at all - see
    # test_dashboard_renders_when_the_database_has_never_been_created.

    monkeypatch.setenv("DB_PATH", str(db_path))
    monkeypatch.setenv("PROFILE_PATH", profile_path)
    # Forces the credentials check in lifespan() to skip starting the
    # collector - these tests are about the dashboard route, not the
    # collector, and B1 means the route must work either way.
    monkeypatch.setenv("EBAY_CLIENT_ID", "")
    monkeypatch.setenv("EBAY_CLIENT_SECRET", "")

    return TestClient(main_module.app)


# ---------------------------------------------------------------------------
# Basic rendering
# ---------------------------------------------------------------------------


def test_dashboard_returns_200_with_recognizable_panel_content(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        r = client.get("/")

    assert r.status_code == 200
    assert "DealWatch" in r.text
    assert "Status" in r.text
    assert "Budget" in r.text
    assert "Alerts per day" in r.text
    assert "Recent alerts" in r.text
    assert "Recent listings" in r.text
    assert "Complete buckets with computed baselines" in r.text
    assert "Test listing" in r.text


def test_header_includes_profile_id(tmp_path, monkeypatch):
    # B1: the page is currently ambiguous about what it reports on - one
    # header line, no profile switcher, no multi-profile UI (out of
    # scope, per design.md §13's V0.11a addendum).
    with _make_client(tmp_path, monkeypatch) as client:
        r = client.get("/")

    assert r.status_code == 200
    assert "DealWatch &mdash; thinkpad-t14" in r.text


def test_health_endpoint_unchanged(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as client:
        r = client.get("/health")

    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"status", "budget", "collector"}
    assert body["status"] == "ok"
    assert set(body["budget"].keys()) == {"period", "used", "ceiling", "remaining"}
    assert set(body["collector"].keys()) == {
        "last_poll_at", "last_sweep_at", "poll_count", "sweep_count",
        "mapping_error_count", "normalize_error_count", "cycle_error_count",
    }


# ---------------------------------------------------------------------------
# B2: the route must be sync def
# ---------------------------------------------------------------------------


def test_route_handler_is_sync_not_async():
    # This looks pedantic and it is the guard against someone
    # "modernizing" this to async def later and quietly stalling the
    # collector - see the route's own docstring in main.py.
    route = next(r for r in main_module.app.routes if getattr(r, "path", None) == "/")
    assert not inspect.iscoroutinefunction(route.endpoint)


# ---------------------------------------------------------------------------
# C7: per-section error isolation
# ---------------------------------------------------------------------------


def test_a_failing_section_renders_an_error_box_and_page_still_returns_200(tmp_path, monkeypatch):
    from dealwatch.reporting import panels

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated panel failure")

    monkeypatch.setattr(panels, "recent_alerts", _boom)

    with _make_client(tmp_path, monkeypatch) as client:
        r = client.get("/")

    assert r.status_code == 200
    assert "Panel unavailable" in r.text
    assert "RuntimeError" in r.text
    assert "simulated panel failure" in r.text
    # Every other panel still rendered.
    assert "Recent listings" in r.text
    assert "Complete buckets with computed baselines" in r.text
    assert "Alerts per day" in r.text


# ---------------------------------------------------------------------------
# C2: the unknown treatment
# ---------------------------------------------------------------------------


def test_dashboard_renders_when_the_database_has_never_been_created(tmp_path, monkeypatch):
    # Live-Docker-verified failure mode: a container with no eBay
    # credentials never starts the collector, and if nothing else has
    # ever created data/dealwatch.db (no /health hit, no prior collector
    # run), connect_readonly() itself fails before build_payload() gets a
    # chance to isolate anything. The route must still return 200 - and
    # since V0.11a's Part A, this renders ONE database-unavailable
    # banner, not six per-panel "Panel unavailable" boxes.
    with _make_client(tmp_path, monkeypatch, create_db=False) as client:
        r = client.get("/")

    assert r.status_code == 200
    assert "DealWatch" in r.text
    assert r.text.count("Database unavailable") == 1  # rendered once, not per panel
    assert r.text.count("Panel unavailable") == 0


def test_unknown_indicator_renders_grey_dash_not_a_value(tmp_path, monkeypatch):
    # An empty, never-swept, never-budgeted database - every
    # unknown-capable indicator is genuinely unevaluable here, so this
    # must render the dash treatment, never a fabricated "ok" or number.
    with _make_client(tmp_path, monkeypatch, seed=False) as client:
        r = client.get("/")

    assert r.status_code == 200
    assert '<span class="dot unknown"></span>' in r.text
    assert "&ndash;" in r.text


# ---------------------------------------------------------------------------
# B3: template resolution must not depend on process cwd
# ---------------------------------------------------------------------------


def test_dashboard_renders_regardless_of_process_cwd(tmp_path, monkeypatch):
    # Jinja2Templates is constructed from Path(__file__).parent - a
    # relative "templates" path would resolve against the process's cwd
    # instead, which works by accident when pytest happens to run from
    # the repo root and breaks under any other cwd (Docker's CMD, a
    # systemd unit, a CI runner). Actually changing directory here, not
    # just asserting on the construction line, is what makes this a real
    # regression test rather than a restatement of the source.
    other_dir = tmp_path / "somewhere_else"
    other_dir.mkdir()
    monkeypatch.chdir(other_dir)

    with _make_client(tmp_path, monkeypatch) as client:
        r = client.get("/")

    assert r.status_code == 200
    assert "DealWatch" in r.text
