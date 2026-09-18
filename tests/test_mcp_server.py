"""Tests for dealwatch.mcp_server.server (V1.0 prompt 1, design.md §15).

Server module-level state (profile, compiled_seeds, allowed hosts, `mcp`,
`app`) is built ONCE at import time, matching D8/D13's "fixed at process
start" intent - a real deploy needs a restart to change any of it, same as
main.py's own `profile`/`templates`. This file sets the env vars that
control that state, clears get_settings's cache, and imports the module
exactly once at collection time; every test after that calls the
already-registered tool functions directly (the `@mcp.tool()` decorator
returns the original callable unchanged - confirmed empirically before
writing these tests) rather than always going through the HTTP/JSON-RPC
layer, except test 13 which explicitly proves that layer.

db_path is the one thing every test needs to vary. _readonly_conn() and
get_system_health() both read it via a FRESH get_settings() call rather
than a module-level capture (see server.py's own docstring on that) - the
same escape hatch main.py's dashboard() route uses - so a test only needs
to monkeypatch DB_PATH and clear get_settings's cache before calling an
already-imported tool function.

Real SQLite via the real connect() (so migrations run), hand-seeded rows
directly against listings/observations/alerts - same approach
tests/test_status.py and tests/test_panels.py already use, for the same
reason: full control over exact field values (gone_at, lifespan_mins,
spec_json, raw_json) that a fixture built through the write path alone
would be awkward to force into a precise shape.
"""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from dealwatch.config import get_settings

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = str(REPO_ROOT / "profiles" / "thinkpad-t14.yaml")

# Set before the FIRST import of dealwatch.mcp_server.server anywhere in
# this test session - its module-level code reads Settings once, at
# import time (D8/D13), so these must be in place and the settings cache
# cleared before that import happens. "testserver" is TestClient's own
# default Host header; "192.168.99.204:8088" stands in for a real LAN
# deploy's allowed host for the host-validation test.
os.environ["PROFILE_PATH"] = PROFILE_PATH
os.environ["MCP_ALLOWED_HOSTS"] = "testserver,192.168.99.204:8088,127.0.0.1:*,localhost:*"
os.environ.setdefault("DB_PATH", str(REPO_ROOT / "data" / "dealwatch.db"))
get_settings.cache_clear()

import dealwatch.mcp_server.server as mcp_server  # noqa: E402
from dealwatch.storage.sqlite import connect  # noqa: E402

HEADERS = {"content-type": "application/json", "accept": "application/json, text/event-stream"}


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _use_db(monkeypatch, db_path) -> None:
    """Point the already-imported server module's tools at db_path for
    the duration of one test, via the fresh-get_settings() escape hatch
    both _readonly_conn() and get_system_health() use."""
    monkeypatch.setenv("DB_PATH", str(db_path))
    get_settings.cache_clear()


def _make_db(tmp_path) -> Path:
    db_path = tmp_path / "dealwatch.db"
    connect(db_path).close()
    return db_path


# ---------------------------------------------------------------------------
# Seed helpers - hand-written INSERTs for precise control, matching
# tests/test_status.py's and tests/test_panels.py's own approach.
# ---------------------------------------------------------------------------

PROFILE_ID = "thinkpad-t14"


def seed_listing(
    conn,
    item_id,
    *,
    profile_id=PROFILE_ID,
    title="t",
    seller=None,
    condition_id=None,
    spec_json=None,
    spec_status="ok",
    reject_rule_id=None,
    bucket_key=None,
    first_seen=1_000_000,
    last_seen=1_000_000,
    gone_at=None,
    lifespan_mins=None,
    variation_id=None,
    item_web_url="https://example.com/listing",
):
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, seller, condition_id, "
        "spec_json, spec_status, reject_rule_id, bucket_key, first_seen, last_seen, "
        "miss_count, gone_at, lifespan_mins, variation_id, item_web_url) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (
            item_id, profile_id, title, seller, condition_id, spec_json, spec_status,
            reject_rule_id, bucket_key, first_seen, last_seen, gone_at, lifespan_mins,
            variation_id, item_web_url,
        ),
    )


def seed_observation(
    conn, item_id, observed_at, *, price_cents=None, total_cents=None,
    shipping_cents=None, raw_json="{}",
):
    conn.execute(
        "INSERT INTO observations (item_id, observed_at, price_cents, total_cents, "
        "shipping_cents, buying_options, raw_json) VALUES (?, ?, ?, ?, ?, '[]', ?)",
        (item_id, observed_at, price_cents, total_cents, shipping_cents, raw_json),
    )


def seed_alert(
    conn, item_id, *, sent_at, dry_run=False, notifier="discord",
    delivery_status="sent", ratio_to_p25=0.9, baseline_layer="seed",
    price_cents=9000, profile_id=PROFILE_ID,
):
    conn.execute(
        "INSERT INTO alerts (item_id, profile_id, sent_at, dry_run, price_cents, "
        "price_is_price_only, bucket_key, baseline_layer, baseline_match, baseline_n, "
        "baseline_p25_cents, baseline_p50_cents, ratio_to_p25, sanity_flagged, "
        "delivery_status, notifier) VALUES (?, ?, ?, ?, ?, 0, '1|intel-10th|16', "
        "?, '{}', NULL, 10000, 15000, ?, 0, ?, ?)",
        (
            item_id, profile_id, sent_at, 1 if dry_run else 0, price_cents,
            baseline_layer, ratio_to_p25, delivery_status, notifier,
        ),
    )


def seed_sweep(conn, *, swept_at, profile_id=PROFILE_ID, distinct_count=10, sweep_recorded=True):
    conn.execute(
        "INSERT INTO sweeps (profile_id, swept_at, fetched_count, distinct_count, "
        "active_count_before, truncated, sweep_recorded) VALUES (?, ?, ?, ?, ?, 0, ?)",
        (profile_id, swept_at, distinct_count, distinct_count, distinct_count, 1 if sweep_recorded else 0),
    )


# ---------------------------------------------------------------------------
# 1. Tools are sync (design.md §15 D7)
# ---------------------------------------------------------------------------


def test_every_registered_tool_is_a_sync_function():
    # D7: a sync tool runs via anyio.to_thread.run_sync; an async one runs
    # on this server's own event loop and would serialize every concurrent
    # tool call (and /health) behind one slow query. `Tool.is_async` is
    # the SDK's own flag for this - confirmed empirically (before writing
    # this test) that it correctly reads True for a throwaway async tool
    # and False for a sync one on a scratch MCPServer instance.
    #
    # Sabotage performed manually (not as an automated test, to avoid
    # mutating the shared `mcp` singleton every other test in this file
    # depends on): changed `def get_system_health():` to
    # `async def get_system_health():` in server.py, re-ran this test in
    # a fresh subprocess, confirmed it failed, then reverted. See the
    # report for the red/green transcript.
    tools = mcp_server.mcp._tool_manager.list_tools()
    assert {t.name for t in tools} == {"get_system_health", "trace_title", "explain_listing"}
    for tool in tools:
        assert not tool.is_async, f"{tool.name} is registered as an async tool"


# ---------------------------------------------------------------------------
# 2. No dealwatch.main import (design.md §15 D2)
# ---------------------------------------------------------------------------


def test_importing_server_never_imports_dealwatch_main():
    # Subprocess, because THIS test process may already have
    # dealwatch.main in sys.modules from an earlier test file (e.g.
    # test_dashboard_route.py) - that would make the assertion
    # meaningless if checked in-process.
    script = (
        "import sys, os\n"
        f"os.environ['PROFILE_PATH'] = {PROFILE_PATH!r}\n"
        "import dealwatch.mcp_server.server\n"
        "assert 'dealwatch.main' not in sys.modules, sorted(sys.modules)\n"
        "print('OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# 3. Host validation (design.md §15 D8)
# ---------------------------------------------------------------------------


def test_host_validation_allows_a_configured_host_and_rejects_others():
    # The app's REAL TransportSecuritySettings construction, built from
    # the real Settings.mcp_allowed_hosts at import time - not a
    # test-only copy.
    with TestClient(mcp_server._build_app()) as client:
        allowed = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={**HEADERS, "Host": "192.168.99.204:8088"},
        )
        assert allowed.status_code == 200

        rejected = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={**HEADERS, "Host": "evil.example"},
        )
        assert rejected.status_code == 421


def test_host_validation_sabotage_transport_security_none_lets_a_bad_host_through():
    # Sabotage (SDK fact 3): reconstruct the SAME MCPServer's
    # streamable_http_app with transport_security=None instead of the
    # real setting, host="0.0.0.0" - this combination disables host
    # protection ENTIRELY (as opposed to host="127.0.0.1" defaulting to
    # localhost-only). This does not edit server.py; it demonstrates that
    # the explicit TransportSecuritySettings construction in server.py is
    # actually load-bearing, by showing what happens without it.
    sabotaged_app = mcp_server.mcp.streamable_http_app(
        stateless_http=True, json_response=True, transport_security=None, host="0.0.0.0"
    )
    with TestClient(sabotaged_app) as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={**HEADERS, "Host": "evil.example"},
        )
        assert r.status_code == 200  # red: the bad-host case now succeeds


# ---------------------------------------------------------------------------
# 4. Read-only connection helper (design.md §15 D4/D5)
# ---------------------------------------------------------------------------


def test_readonly_helper_rejects_a_write(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    with pytest.raises(sqlite3.OperationalError):
        with mcp_server._readonly_conn() as conn:
            conn.execute(
                "INSERT INTO listings (item_id, profile_id, title, spec_status, "
                "first_seen, last_seen, miss_count) VALUES ('x', 'p', 't', 'ok', 1, 1, 0)"
            )


def test_readonly_helper_sabotage_swapping_to_connect_allows_writes(tmp_path, monkeypatch):
    # Sabotage: swap the helper's connect_readonly() for the real,
    # writable connect() - the write that test 4 expects to raise must
    # now succeed, proving the real test only passes because of the
    # real (read-only) helper.
    from dealwatch.storage import sqlite as storage_sqlite

    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)
    monkeypatch.setattr(mcp_server, "connect_readonly", storage_sqlite.connect)

    with mcp_server._readonly_conn() as conn:
        conn.execute(
            "INSERT INTO listings (item_id, profile_id, title, spec_status, "
            "first_seen, last_seen, miss_count) VALUES ('x', 'p', 't', 'ok', 1, 1, 0)"
        )
        conn.commit()
    # No exception raised above - red confirmed.


# ---------------------------------------------------------------------------
# 5. /health reads the database (design.md §15 D9)
# ---------------------------------------------------------------------------


def test_health_returns_200_with_schema_version_against_a_migrated_db(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    with TestClient(mcp_server._build_app()) as client:
        r = client.get("/health")

    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["schema_version"] == 8
    assert body["profile_id"] == PROFILE_ID


def test_health_returns_503_against_a_nonexistent_database(tmp_path, monkeypatch):
    _use_db(monkeypatch, tmp_path / "never-created.db")

    with TestClient(mcp_server._build_app()) as client:
        r = client.get("/health")

    assert r.status_code == 503
    assert "status" in r.json() and r.json()["status"] == "error"


def test_health_sabotage_a_static_200_masks_a_real_database_failure(tmp_path, monkeypatch):
    # Sabotage: bypass the actual database check entirely.
    async def _fake_schema_version():
        return 8

    monkeypatch.setattr(mcp_server, "_read_schema_version", _fake_schema_version)
    _use_db(monkeypatch, tmp_path / "never-created.db")

    with TestClient(mcp_server._build_app()) as client:
        r = client.get("/health")

    assert r.status_code == 200  # red: masks the nonexistent database


# ---------------------------------------------------------------------------
# 6. NULL lifespan is never rendered as zero (CLAUDE.md's lifespan_mins trap)
# ---------------------------------------------------------------------------


def test_explain_listing_null_lifespan_renders_the_unmeasured_state(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|111|0", spec_status="ok", bucket_key="1|intel-10th|16",
        gone_at=2_000_000, lifespan_mins=None,
    )
    seed_observation(conn, "v1|111|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|111|0")

    assert result["found"] is True
    assert result["timeline"]["duration"]["state"] == "unmeasured"
    assert "lifespan_mins" not in result["timeline"]["duration"]
    assert 0 not in result["timeline"]["duration"].values()


def test_explain_listing_zero_lifespan_is_a_real_measurement_not_unmeasured(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|222|0", spec_status="ok", bucket_key="1|intel-10th|16",
        gone_at=2_000_000, lifespan_mins=0,
    )
    seed_observation(conn, "v1|222|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|222|0")

    assert result["timeline"]["duration"]["state"] == "measured"
    assert result["timeline"]["duration"]["lifespan_mins"] == 0


def test_explain_listing_sabotage_coalesce_lifespan_to_zero(tmp_path, monkeypatch):
    # Sabotage: COALESCE the NULL to 0 before the state check runs -
    # reproduced here by monkeypatching _timeline_section with the
    # sabotaged logic inline, since editing the SQL isn't where this
    # value comes from (it's a plain column read, no query to alter).
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|333|0", spec_status="ok", bucket_key="1|intel-10th|16",
        gone_at=2_000_000, lifespan_mins=None,
    )
    seed_observation(conn, "v1|333|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    original = mcp_server._timeline_section

    def _sabotaged(row, observations, now):
        # Recreate `row` with lifespan_mins coalesced to 0 before delegating.
        patched = dict(row)
        patched["lifespan_mins"] = patched["lifespan_mins"] or 0
        return original(patched, observations, now)

    monkeypatch.setattr(mcp_server, "_timeline_section", _sabotaged)
    result = mcp_server.explain_listing(item_id_or_url="v1|333|0")

    assert result["timeline"]["duration"]["state"] == "measured"  # red: should be "unmeasured"
    assert result["timeline"]["duration"]["lifespan_mins"] == 0


# ---------------------------------------------------------------------------
# 7. matches_stored (design.md §15 tool 4)
# ---------------------------------------------------------------------------

# Confirmed against the REAL profile before writing this fixture (not
# assumed): `python -m dealwatch.normalize.explain --profile
# profiles/thinkpad-t14.yaml --title "Lenovo ThinkPad T14s Gen 2 16GB
# 256GB"` -> spec_status=rejected, reject_rule_id=t14s-not-t14.
_T14S_TITLE = "Lenovo ThinkPad T14s Gen 2 16GB 256GB"


def _seed_for_matches_stored(conn, item_id, *, stored_spec_status, stored_reject_rule_id, stored_bucket_key):
    seed_listing(
        conn, item_id, title=_T14S_TITLE, spec_status=stored_spec_status,
        reject_rule_id=stored_reject_rule_id, bucket_key=stored_bucket_key,
        spec_json=json.dumps({}),
    )
    seed_observation(
        conn, item_id, 1_000_000, price_cents=10000,
        raw_json=json.dumps({"title": _T14S_TITLE}),
    )


def test_explain_listing_matches_stored_true_when_stored_agrees_with_a_fresh_run(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    _seed_for_matches_stored(
        conn, "v1|444|0", stored_spec_status="rejected",
        stored_reject_rule_id="t14s-not-t14", stored_bucket_key=None,
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|444|0")

    assert result["normalization"]["fresh"]["reject_rule_id"] == "t14s-not-t14"
    assert result["normalization"]["matches_stored"] is True


def test_explain_listing_matches_stored_false_when_the_profile_has_changed_since(tmp_path, monkeypatch):
    # Stored as if an OLDER profile (before t14s-not-t14 existed) had
    # normalized this row as an ordinary ok T14 - the CURRENT profile
    # rejects it, so matches_stored must be False.
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    _seed_for_matches_stored(
        conn, "v1|555|0", stored_spec_status="ok",
        stored_reject_rule_id=None, stored_bucket_key="2|?|16",
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|555|0")

    assert result["normalization"]["fresh"]["reject_rule_id"] == "t14s-not-t14"
    assert result["normalization"]["matches_stored"] is False


def test_explain_listing_sabotage_matches_stored_hardcoded_true(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    _seed_for_matches_stored(
        conn, "v1|666|0", stored_spec_status="ok",
        stored_reject_rule_id=None, stored_bucket_key="2|?|16",
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    original = mcp_server._normalization_section

    def _sabotaged(row, observations):
        section = original(row, observations)
        section["matches_stored"] = True  # hardcoded, ignoring the real comparison
        return section

    monkeypatch.setattr(mcp_server, "_normalization_section", _sabotaged)
    result = mcp_server.explain_listing(item_id_or_url="v1|666|0")

    assert result["normalization"]["matches_stored"] is True  # red: should be False


# ---------------------------------------------------------------------------
# 8. Re-normalization uses normalize_input_fields, not title alone
# ---------------------------------------------------------------------------

# Confirmed against the real profile: the same title normalizes to
# spec_status=ok with no condition_id, but spec_status=rejected
# (for-parts-condition) when condition_id=7000 is present - a structured
# raw_json field the title alone cannot carry.
_CONDITION_TITLE = "Lenovo ThinkPad T14 Gen 1 Intel i5-10310U 16GB 256GB SSD"


def test_explain_listing_renormalization_reads_condition_id_from_raw_json(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|777|0", title=_CONDITION_TITLE, spec_status="rejected",
        reject_rule_id="for-parts-condition", bucket_key=None, spec_json=json.dumps({}),
    )
    seed_observation(
        conn, "v1|777|0", 1_000_000, price_cents=10000,
        raw_json=json.dumps({"title": _CONDITION_TITLE, "conditionId": "7000"}),
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|777|0")

    assert result["normalization"]["fresh"]["spec_status"] == "rejected"
    assert result["normalization"]["fresh"]["reject_rule_id"] == "for-parts-condition"
    assert result["normalization"]["matches_stored"] is True


def test_explain_listing_sabotage_renormalizing_from_title_only(tmp_path, monkeypatch):
    # Sabotage: build listing_fields from the title alone, dropping the
    # structured raw_json fields normalize_input_fields() would supply.
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|888|0", title=_CONDITION_TITLE, spec_status="rejected",
        reject_rule_id="for-parts-condition", bucket_key=None, spec_json=json.dumps({}),
    )
    seed_observation(
        conn, "v1|888|0", 1_000_000, price_cents=10000,
        raw_json=json.dumps({"title": _CONDITION_TITLE, "conditionId": "7000"}),
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    from dealwatch.normalize.engine import normalize_verbose as real_normalize_verbose

    def _title_only_normalize_verbose(profile, listing_fields):
        return real_normalize_verbose(profile, {"title": listing_fields["title"]})

    monkeypatch.setattr(mcp_server, "normalize_verbose", _title_only_normalize_verbose)
    result = mcp_server.explain_listing(item_id_or_url="v1|888|0")

    assert result["normalization"]["fresh"]["spec_status"] == "ok"  # red: should be "rejected"


# ---------------------------------------------------------------------------
# 9. trace_title parity with normalize/explain.py
# ---------------------------------------------------------------------------


def test_trace_title_matches_normalize_verbose_directly():
    from dealwatch.normalize.engine import normalize_verbose

    expected_result, expected_trace = normalize_verbose(
        mcp_server.profile,
        {"title": _CONDITION_TITLE, "subtitle": None, "condition_id": 7000},
    )

    tool_result = mcp_server.trace_title(title=_CONDITION_TITLE, condition_id=7000)

    assert tool_result["spec_status"] == expected_result.spec_status == "rejected"
    assert tool_result["reject_rule_id"] == expected_result.reject_rule_id == "for-parts-condition"
    assert tool_result["bucket_key"] == expected_result.bucket_key
    assert tool_result["trace"] == expected_trace


def test_trace_title_sabotage_dropping_condition_id_from_the_input_dict():
    # Sabotage: drop condition_id from the dict passed to
    # normalize_verbose() - for THIS fixture, condition_id=7000 is what
    # causes the rejection, so dropping it changes the outcome.
    from dealwatch.normalize.engine import normalize_verbose as real_normalize_verbose

    def _dropped_condition_id(profile, listing_fields):
        return real_normalize_verbose(profile, {"title": listing_fields["title"]})

    import unittest.mock

    with unittest.mock.patch.object(mcp_server, "normalize_verbose", _dropped_condition_id):
        result = mcp_server.trace_title(title=_CONDITION_TITLE, condition_id=7000)

    assert result["spec_status"] == "ok"  # red: should be "rejected"


# ---------------------------------------------------------------------------
# 10. Health rollup is not re-derived
# ---------------------------------------------------------------------------


def test_get_system_health_rollup_equals_build_indicators_for_the_same_status(tmp_path, monkeypatch):
    from dealwatch.reporting.indicators import build_indicators
    from dealwatch.reporting.status import collect_status

    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_sweep(conn, swept_at=500_000, distinct_count=10)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.get_system_health()

    live_settings = get_settings()
    ceiling = live_settings.daily_call_limit - live_settings.daily_reserve_calls
    conn = mcp_server.connect_readonly(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        status = collect_status(conn, PROFILE_ID, ceiling=ceiling, now=result["as_of"])
    finally:
        conn.close()
    expected_indicators = build_indicators(
        status,
        sweep_interval_minutes=mcp_server.profile.search.poll.sweep_interval_minutes,
        dry_run=mcp_server.profile.alerts.dry_run if mcp_server.profile.alerts else True,
        notifiers=mcp_server.profile.alerts.notifiers if mcp_server.profile.alerts else [],
    )

    assert result["collector"] == expected_indicators["collector"]
    assert result["sweep_coverage"] == expected_indicators["sweep_coverage"]
    assert result["bookkeeping"] == expected_indicators["bookkeeping"]


def test_get_system_health_sabotage_rederiving_the_rollup_locally(tmp_path, monkeypatch):
    # A sweep started 1 minute ago against a 60-minute interval reads
    # "ok"/"healthy" through the real pipeline - freezing `now` right
    # next to `swept_at` (rather than letting it default to the real
    # wall clock, which would make the sweep look ancient and coincide
    # with the sabotaged "warn" value by chance) is what makes the
    # sabotage's divergence real rather than accidental.
    db_path = _make_db(tmp_path)
    now = 1_000_000
    conn = connect(db_path)
    swept_at = now - 60
    seed_sweep(conn, swept_at=swept_at, distinct_count=1)
    # A listing the sweep properly stamped - without this, bookkeeping
    # has no pre-sweep row to compare against and reads "unknown" (V0.13,
    # design.md §14), which would make the ROLLUP "unknown" regardless of
    # sweep age/coverage and defeat this test's own point.
    seed_listing(conn, "v1|1|0", first_seen=swept_at - 500, last_seen=swept_at)
    conn.close()
    _use_db(monkeypatch, db_path)

    import time as time_module

    monkeypatch.setattr(time_module, "time", lambda: float(now))

    # Sabotage: compute a "collector" verdict locally with a changed
    # (much stricter) threshold instead of reading build_indicators()'s.
    real_get = mcp_server.get_system_health

    def _sabotaged():
        result = real_get()
        result["collector"] = {"state": "warn", "label": "Collector", "value": "degraded", "group": "health"}
        return result

    result = _sabotaged()
    from dealwatch.reporting.indicators import build_indicators
    from dealwatch.reporting.status import collect_status

    live_settings = get_settings()
    ceiling = live_settings.daily_call_limit - live_settings.daily_reserve_calls
    conn = mcp_server.connect_readonly(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        status = collect_status(conn, PROFILE_ID, ceiling=ceiling, now=now)
    finally:
        conn.close()
    expected = build_indicators(
        status, sweep_interval_minutes=mcp_server.profile.search.poll.sweep_interval_minutes,
        dry_run=True, notifiers=[],
    )
    assert expected["collector"]["state"] == "ok"  # the real value, unsabotaged
    assert result["collector"] != expected["collector"]  # red: rollup no longer matches


# ---------------------------------------------------------------------------
# 11. Next sweep overdue, never negative
# ---------------------------------------------------------------------------


def test_get_system_health_next_sweep_reads_overdue_not_a_negative_future_time(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    now = 1_000_000
    sweep_interval_minutes = mcp_server.profile.search.poll.sweep_interval_minutes
    # Last sweep started well over TWO intervals ago - unambiguously overdue.
    swept_at = now - sweep_interval_minutes * 60 * 3
    seed_sweep(conn, swept_at=swept_at, distinct_count=10)
    conn.close()
    _use_db(monkeypatch, db_path)

    import time as time_module

    monkeypatch.setattr(time_module, "time", lambda: float(now))
    result = mcp_server.get_system_health()

    assert result["next_sweep"]["state"] == "overdue"
    assert "-" not in result["next_sweep"]["display"]
    assert result["next_sweep"]["display"].startswith("overdue by")


def test_next_sweep_sabotage_removing_the_past_check_branch():
    # Sabotage: always compute "in N min" from estimated_at - now, with
    # no branch for the overdue case.
    def _sabotaged_next_sweep(last_sweep_started_at, sweep_interval_minutes, now):
        if last_sweep_started_at is None:
            return {"state": "unknown", "estimated_at": None, "display": None}
        estimated_at = last_sweep_started_at + sweep_interval_minutes * 60
        in_min = (estimated_at - now) // 60
        return {"state": "pending", "estimated_at": estimated_at, "display": f"in {in_min} min"}

    now = 1_000_000
    result = _sabotaged_next_sweep(now - 7200, 60, now)  # two intervals ago - overdue by one full interval
    assert "-" in result["display"]  # red: "in -60 min"


# ---------------------------------------------------------------------------
# 12. Alert history includes dry-run and per-notifier rows
# ---------------------------------------------------------------------------


def test_explain_listing_alert_history_includes_dry_run_and_every_notifier(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|999|0", spec_status="ok", bucket_key="1|intel-10th|16")
    seed_observation(conn, "v1|999|0", 1_000_000, price_cents=10000)
    seed_alert(conn, "v1|999|0", sent_at=1_100_000, dry_run=True, notifier="discord")
    seed_alert(conn, "v1|999|0", sent_at=1_200_000, dry_run=False, notifier="discord")
    seed_alert(conn, "v1|999|0", sent_at=1_200_000, dry_run=False, notifier="pushover")
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|999|0")

    history = result["alert_history"]
    assert len(history) == 3
    assert any(row["dry_run"] is True for row in history)
    assert {row["notifier"] for row in history} == {"discord", "pushover"}
    # Newest first.
    assert history[0]["sent_at"] == 1_200_000
    assert history[-1]["sent_at"] == 1_100_000


def test_explain_listing_alert_history_sabotage_filtering_dry_run(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|1000|0", spec_status="ok", bucket_key="1|intel-10th|16")
    seed_observation(conn, "v1|1000|0", 1_000_000, price_cents=10000)
    seed_alert(conn, "v1|1000|0", sent_at=1_100_000, dry_run=True, notifier="discord")
    seed_alert(conn, "v1|1000|0", sent_at=1_200_000, dry_run=False, notifier="discord")
    conn.close()
    _use_db(monkeypatch, db_path)

    def _sabotaged(conn, item_id):
        rows = conn.execute(
            "SELECT sent_at, dry_run, notifier, delivery_status, price_cents, "
            "ratio_to_p25, baseline_layer FROM alerts WHERE item_id = ? AND dry_run = 0 "
            "ORDER BY sent_at DESC, id DESC",
            (item_id,),
        ).fetchall()
        return [
            {
                "sent_at": r["sent_at"], "dry_run": bool(r["dry_run"]), "notifier": r["notifier"],
                "delivery_status": r["delivery_status"], "price_cents": r["price_cents"],
                "ratio_to_p25": r["ratio_to_p25"], "baseline_layer": r["baseline_layer"],
            }
            for r in rows
        ]

    monkeypatch.setattr(mcp_server, "_alert_history_section", _sabotaged)
    result = mcp_server.explain_listing(item_id_or_url="v1|1000|0")

    assert len(result["alert_history"]) == 1  # red: should be 2


# ---------------------------------------------------------------------------
# 13. End-to-end over HTTP
# ---------------------------------------------------------------------------


def _call_over_http(client, name, arguments, *, request_id):
    return client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        headers=HEADERS,
    )


def test_end_to_end_tools_list_and_explain_listing_over_http(tmp_path, monkeypatch):
    # Not sabotage-checked the same way the tests above are - this proves
    # wiring (transport, JSON-RPC framing, the SDK's own dispatch), not a
    # specific piece of this module's logic, so there is no single
    # "wrong" behavior to sabotage into it.
    #
    # Real gap this closes: tests 6-12 all call the tool functions
    # directly in Python (the `@mcp.tool()` decorator returns the
    # original callable unchanged), which proves the LOGIC is right but
    # never proves the SDK can actually SERIALIZE what that logic
    # returns. A tool can return a perfectly good Python dict that still
    # fails at the JSON-RPC boundary - a sqlite3.Row, a datetime, a value
    # the SDK's derived output schema doesn't expect - and every
    # direct-call test above would stay green while a real client got
    # `isError: true`. get_system_health was the one under real
    # suspicion (it wraps collect_status()'s payload, built for Jinja and
    # a CLI, not JSON) - it was checked here and does serialize cleanly,
    # but that was previously confirmed only by a throwaway manual probe,
    # never by a committed test. Every tool this milestone ships is now
    # called over the real HTTP/JSON-RPC layer at least once, asserting
    # both `isError: false` and that the content text is valid JSON.
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|1111|0", spec_status="ok", bucket_key="1|intel-10th|16")
    seed_observation(conn, "v1|1111|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    with TestClient(mcp_server._build_app()) as client:
        list_response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers=HEADERS,
        )
        assert list_response.status_code == 200
        tool_names = {t["name"] for t in list_response.json()["result"]["tools"]}
        assert tool_names == {"get_system_health", "trace_title", "explain_listing"}

        found_response = _call_over_http(
            client, "explain_listing", {"item_id_or_url": "v1|1111|0"}, request_id=2
        )
        assert found_response.status_code == 200
        found_result = found_response.json()["result"]
        assert found_result["isError"] is False
        found_payload = json.loads(found_result["content"][0]["text"])
        assert found_payload["found"] is True
        assert "as_of" in found_payload
        assert found_payload["profile_id"] == PROFILE_ID

        not_found_response = _call_over_http(
            client, "explain_listing", {"item_id_or_url": "v1|9999999999|0"}, request_id=3
        )
        assert not_found_response.status_code == 200
        not_found_result = not_found_response.json()["result"]
        assert not_found_result["isError"] is False
        not_found_payload = json.loads(not_found_result["content"][0]["text"])
        assert not_found_payload["found"] is False

        health_response = _call_over_http(client, "get_system_health", {}, request_id=4)
        assert health_response.status_code == 200
        health_result = health_response.json()["result"]
        assert health_result["isError"] is False
        health_payload = json.loads(health_result["content"][0]["text"])
        assert health_payload["profile_id"] == PROFILE_ID
        assert "collector" in health_payload

        trace_response = _call_over_http(
            client, "trace_title",
            {"title": "Lenovo ThinkPad T14 Gen 1 16GB 256GB"},
            request_id=5,
        )
        assert trace_response.status_code == 200
        trace_result = trace_response.json()["result"]
        assert trace_result["isError"] is False
        trace_payload = json.loads(trace_result["content"][0]["text"])
        assert trace_payload["profile_id"] == PROFILE_ID
        assert "trace" in trace_payload


# ---------------------------------------------------------------------------
# Extra coverage beyond the 13 required tests - item resolution and the
# score section's skip/compute branches, both load-bearing parts of
# explain_listing() not exercised by any test above.
# ---------------------------------------------------------------------------


def test_explain_listing_resolves_a_full_item_id_directly(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|2222|0", spec_status="ok", bucket_key="1|intel-10th|16")
    seed_observation(conn, "v1|2222|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|2222|0")
    assert result["found"] is True
    assert result["identity"]["item_id"] == "v1|2222|0"


def test_explain_listing_resolves_a_url_to_its_legacy_item_number(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|333344445555|0", spec_status="ok", bucket_key="1|intel-10th|16")
    seed_observation(conn, "v1|333344445555|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(
        item_id_or_url="https://www.ebay.com/itm/some-title-slug/333344445555?hash=abc"
    )
    assert result["found"] is True
    assert result["identity"]["item_id"] == "v1|333344445555|0"


def test_explain_listing_bare_number_matching_multiple_variations_returns_all_candidates(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|444455556666|1", spec_status="ok", variation_id="1")
    seed_listing(conn, "v1|444455556666|2", spec_status="ok", variation_id="2")
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="444455556666")

    assert result["found"] is False
    assert result["multiple_matches"] is True
    assert set(result["candidate_item_ids"]) == {"v1|444455556666|1", "v1|444455556666|2"}


def test_explain_listing_not_found_is_data_not_an_exception(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|9999999999|0")
    assert result == {
        "found": False,
        "as_of": result["as_of"],
        "profile_id": PROFILE_ID,
        "item_id_or_url": "v1|9999999999|0",
        "item_id": "v1|9999999999|0",
    }


def test_explain_listing_score_is_skipped_for_a_gone_listing(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|5555|0", spec_status="ok", bucket_key="1|intel-10th|16", gone_at=2_000_000,
        lifespan_mins=60,
    )
    seed_observation(conn, "v1|5555|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|5555|0")
    assert result["score"] == {"scored": False, "reason": "not active (gone)"}


def test_explain_listing_score_is_skipped_for_an_incomplete_bucket_key(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|6666|0", spec_status="partial", bucket_key="1|?|16")
    seed_observation(conn, "v1|6666|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|6666|0")
    assert result["score"] == {"scored": False, "reason": "incomplete bucket_key"}


def test_explain_listing_score_is_skipped_for_no_usable_price(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|7777|0", spec_status="ok", bucket_key="1|intel-10th|16")
    seed_observation(conn, "v1|7777|0", 1_000_000, price_cents=None, total_cents=None)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|7777|0")
    assert result["score"] == {"scored": False, "reason": "no usable price"}


def test_explain_listing_computes_a_real_score_against_a_seed_baseline(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|8888|0", spec_status="ok", bucket_key="1|intel-10th|16",
        spec_json=json.dumps({"generation": "1", "cpu_family": "intel-10th"}),
    )
    seed_observation(conn, "v1|8888|0", 1_000_000, price_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.explain_listing(item_id_or_url="v1|8888|0")

    assert result["score"]["scored"] is True
    assert result["score"]["baseline_layer"] in ("seed", "computed")
    assert result["score"]["price_cents"] == 10000
    assert result["score"]["ratio_to_p25"] == pytest.approx(10000 / result["score"]["baseline_p25_cents"])
