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
import time
from datetime import datetime
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


# cpu_family's real vocabulary source is "observed" (vocabulary.py - its
# extract rules mix literal values with two capture-group templates, so
# the rule list alone isn't a safe, complete enumeration) - computed once
# at THIS test session's module-import time from whatever `listings` rows
# exist in the checkout's real data/dealwatch.db, which has no `listings`
# table at all in this checkout (empty file). Every test that exercises a
# real cpu_family value needs it in the vocabulary, so this autouse
# fixture seeds a representative one covering every literal cpu_family
# value this profile's extract rules can produce - the same reasoning
# `_use_db` already applies to DB_PATH: tests set up what they need
# explicitly rather than relying on the checkout's actual file contents.
_TEST_CPU_FAMILIES = [
    "amd-ryzen-4000", "amd-ryzen-5000", "amd-ryzen-6000", "amd-ryzen-7000",
    "amd-ryzen-8000", "amd-ryzen-ai", "intel-10th", "intel-11th", "intel-12th",
    "intel-13th", "intel-ultra-1", "intel-ultra-2",
]


@pytest.fixture(autouse=True)
def _seed_cpu_family_vocabulary(monkeypatch):
    """Seeds mcp_server._observed_vocabulary_cache directly (V1.0 prompt
    2b replaced the old static `_bucket_vocabulary` with a TTL-cached
    accessor, `_current_bucket_vocabulary()`) - a fresh (real
    time.monotonic()) cache entry so ordinary tests never trigger a
    rebuild attempt against this checkout's real, listings-less
    data/dealwatch.db. monkeypatch.setitem restores whatever was there
    (nothing, in a fresh test session) after the test.

    Tests that exercise the TTL/degraded-path mechanics THEMSELVES pop
    "cpu_family" back out at the start of their own body
    (`mcp_server._observed_vocabulary_cache.pop("cpu_family", None)`) -
    monkeypatch still correctly restores the pre-fixture state afterward
    regardless of what a test does to the dict in between.
    """
    monkeypatch.setitem(
        mcp_server._observed_vocabulary_cache,
        "cpu_family",
        (time.monotonic(), int(time.time()), list(_TEST_CPU_FAMILIES)),
    )


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
    price_cents=9000, profile_id=PROFILE_ID, bucket_key="1|intel-10th|16",
    baseline_p25_cents=10000, baseline_p50_cents=15000,
):
    conn.execute(
        "INSERT INTO alerts (item_id, profile_id, sent_at, dry_run, price_cents, "
        "price_is_price_only, bucket_key, baseline_layer, baseline_match, baseline_n, "
        "baseline_p25_cents, baseline_p50_cents, ratio_to_p25, sanity_flagged, "
        "delivery_status, notifier) VALUES (?, ?, ?, ?, ?, 0, ?, "
        "?, '{}', NULL, ?, ?, ?, 0, ?, ?)",
        (
            item_id, profile_id, sent_at, 1 if dry_run else 0, price_cents,
            bucket_key, baseline_layer, baseline_p25_cents, baseline_p50_cents,
            ratio_to_p25, delivery_status, notifier,
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
    assert {t.name for t in tools} == {
        "get_system_health", "query_listings", "find_deals", "trace_title",
        "explain_listing", "get_market_price", "get_alert_activity", "get_review_queue",
    }
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
        assert tool_names == {
            "get_system_health", "query_listings", "find_deals", "trace_title",
            "explain_listing", "get_market_price", "get_alert_activity", "get_review_queue",
        }

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

        # V1.0 prompt 2 - the five new tools, same wire-serialization check:
        # a tool can hand back a perfectly good Python dict that still
        # fails at the JSON-RPC boundary, and every direct-call test
        # elsewhere in this file would stay green while a real client got
        # isError: true. Each is called once over the real HTTP/JSON-RPC
        # layer, asserting isError: false and that the content parses.
        query_response = _call_over_http(client, "query_listings", {}, request_id=6)
        assert query_response.status_code == 200
        query_result = query_response.json()["result"]
        assert query_result["isError"] is False
        query_payload = json.loads(query_result["content"][0]["text"])
        assert query_payload["profile_id"] == PROFILE_ID
        assert "total_count" in query_payload

        deals_response = _call_over_http(client, "find_deals", {}, request_id=7)
        assert deals_response.status_code == 200
        deals_result = deals_response.json()["result"]
        assert deals_result["isError"] is False
        deals_payload = json.loads(deals_result["content"][0]["text"])
        assert deals_payload["profile_id"] == PROFILE_ID
        assert "deals" in deals_payload

        price_response = _call_over_http(
            client, "get_market_price",
            {"generation": "1", "cpu_family": "intel-10th", "ram_tier": "16"},
            request_id=8,
        )
        assert price_response.status_code == 200
        price_result = price_response.json()["result"]
        assert price_result["isError"] is False
        price_payload = json.loads(price_result["content"][0]["text"])
        assert price_payload["profile_id"] == PROFILE_ID
        assert "baseline" in price_payload

        activity_response = _call_over_http(client, "get_alert_activity", {}, request_id=9)
        assert activity_response.status_code == 200
        activity_result = activity_response.json()["result"]
        assert activity_result["isError"] is False
        activity_payload = json.loads(activity_result["content"][0]["text"])
        assert activity_payload["profile_id"] == PROFILE_ID
        assert "recent_events" in activity_payload

        queue_response = _call_over_http(client, "get_review_queue", {}, request_id=10)
        assert queue_response.status_code == 200
        queue_result = queue_response.json()["result"]
        assert queue_result["isError"] is False
        queue_payload = json.loads(queue_result["content"][0]["text"])
        assert queue_payload["profile_id"] == PROFILE_ID
        assert "negative_lifespan_dropped" in queue_payload


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


# ---------------------------------------------------------------------------
# V1.0 prompt 2 - the five remaining tools (design.md §15, tools 2/3/6/7/8).
# Tests numbered per v1.0-prompt-2-mcp-tools.md's own required-test list.
# ---------------------------------------------------------------------------


# --- 1/2/4: query_listings ---------------------------------------------


def test_query_listings_default_includes_every_spec_status(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|q1|0", spec_status="ok", bucket_key="1|intel-10th|16")
    seed_listing(conn, "v1|q2|0", spec_status="partial", bucket_key="1|?|16")
    seed_listing(conn, "v1|q3|0", spec_status="rejected")
    seed_listing(conn, "v1|q4|0", spec_status="not_target")
    conn.close()
    _use_db(monkeypatch, db_path)

    with_default = mcp_server.query_listings()
    assert with_default["total_count"] == 4
    assert with_default["spec_status_counts"] == {
        "ok": 1, "partial": 1, "rejected": 1, "not_target": 1,
    }

    grouped = mcp_server.query_listings(group_by="spec_status")
    assert grouped["spec_status_counts"] == with_default["spec_status_counts"]

    # Manual sabotage (source edit, run, revert - see report): defaulting
    # `spec_status` to `["ok", "partial"]` instead of "every status" drops
    # with_default["total_count"] to 2 and removes "rejected"/"not_target"
    # from the breakdown entirely, red-confirmed and reverted.


def test_query_listings_min_price_excludes_rejected_and_not_target(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    # The cheap rejected barebones row would win the minimum otherwise -
    # design.md §15 tool 2's own caveat example.
    seed_listing(conn, "v1|cheap|0", spec_status="rejected", first_seen=1_000_000)
    seed_observation(conn, "v1|cheap|0", 1_000_000, price_cents=100, total_cents=100)
    seed_listing(conn, "v1|real|0", spec_status="ok", bucket_key="1|intel-10th|16", first_seen=1_000_100)
    seed_observation(conn, "v1|real|0", 1_000_100, price_cents=20000, total_cents=20000)
    conn.close()
    _use_db(monkeypatch, db_path)

    # Filter includes 'rejected' explicitly - it must still not win the minimum.
    result = mcp_server.query_listings(spec_status=["ok", "rejected"])
    assert result["total_count"] == 2
    assert result["price_stats"]["min_cents"] == 20000
    assert result["price_stats"]["min_display"] == "$200.00"

    # Manual sabotage (source edit, run, revert - see report): computing
    # price stats from `matched` instead of `priceable` (i.e. dropping the
    # rejected/not_target exclusion) makes min_cents come back 100 - the
    # rejected row's price - red-confirmed and reverted.


def test_query_listings_today_uses_the_la_calendar_day_not_utc(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    # `now`: 2026-09-19 23:00 America/Los_Angeles = 2026-09-20 06:00 UTC -
    # LA calendar day is the 19th, UTC calendar day is the 20th.
    now = int(datetime(2026, 9, 19, 23, 0, tzinfo=pacific).timestamp())
    # Both rows fall on LA's Sept 19 (inside [Sept19 00:00, Sept20 00:00) LA).
    # In UTC terms: row_early is Sept 19 09:00Z (UTC day 19, NOT today's UTC
    # day of 20), row_late is Sept 20 03:00Z (UTC day 20, matches by luck).
    # A UTC-day query for "today" relative to `now` would only see row_late.
    row_early = int(datetime(2026, 9, 19, 2, 0, tzinfo=pacific).timestamp())
    row_late = int(datetime(2026, 9, 19, 20, 0, tzinfo=pacific).timestamp())

    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|early|0", spec_status="ok", first_seen=row_early, last_seen=row_early)
    seed_listing(conn, "v1|late|0", spec_status="ok", first_seen=row_late, last_seen=row_late)
    conn.close()
    _use_db(monkeypatch, db_path)

    real_time = mcp_server.time
    monkeypatch.setattr(real_time, "time", lambda: float(now))
    result = mcp_server.query_listings(period="today")
    assert result["total_count"] == 2  # both are within the LA calendar day


def test_query_listings_sabotage_today_using_the_utc_day(tmp_path, monkeypatch):
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    now = int(datetime(2026, 9, 19, 23, 0, tzinfo=pacific).timestamp())
    row_early = int(datetime(2026, 9, 19, 2, 0, tzinfo=pacific).timestamp())
    row_late = int(datetime(2026, 9, 19, 20, 0, tzinfo=pacific).timestamp())

    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|early|0", spec_status="ok", first_seen=row_early, last_seen=row_early)
    seed_listing(conn, "v1|late|0", spec_status="ok", first_seen=row_late, last_seen=row_late)
    conn.close()
    _use_db(monkeypatch, db_path)

    def _utc_day_bounds(ts):
        import datetime as dt
        day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).date()
        start = dt.datetime(day.year, day.month, day.day, tzinfo=dt.timezone.utc)
        return int(start.timestamp()), int(start.timestamp()) + 86400

    monkeypatch.setattr(mcp_server, "la_day_bounds", _utc_day_bounds)
    monkeypatch.setattr(mcp_server.time, "time", lambda: float(now))
    result = mcp_server.query_listings(period="today")
    assert result["total_count"] == 1  # red: row_early silently drops out


def test_query_listings_title_contains_is_parameterized(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|special|0", spec_status="ok", title="100% off_deal's laptop")
    conn.close()
    _use_db(monkeypatch, db_path)

    for needle in ("%", "_", "'", "off_deal's"):
        result = mcp_server.query_listings(title_contains=needle)
        assert "error" not in result
        assert result["total_count"] == 1

    # Manual sabotage (source edit, run, revert - see report): f-string
    # interpolating title_contains directly into the SQL string (dropping
    # the `?` parameter) breaks on the embedded single quote with
    # sqlite3.OperationalError - red-confirmed and reverted.


def test_query_listings_period_and_seen_since_is_an_argument_error(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    result = mcp_server.query_listings(period="7d", seen_since=1_000_000)
    assert "error" in result
    assert result["as_of"] > 0
    assert result["profile_id"] == PROFILE_ID


# --- 5/6/7: find_deals ---------------------------------------------------


def test_find_deals_separates_sanity_flagged_from_deals(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    # A sanity-flagged listing at a ridiculous discount would rank #1 among
    # deals if it weren't separated out (design.md §15 tool 3's caveat).
    seed_listing(
        conn, "v1|flagged|0", spec_status="ok", bucket_key="2|intel-11th|16",
        spec_json=json.dumps({"generation": "2", "cpu_family": "intel-11th", "ram_tier": "16"}),
    )
    seed_observation(conn, "v1|flagged|0", 1_000_000, price_cents=100, total_cents=100)
    seed_listing(
        conn, "v1|real|0", spec_status="ok", bucket_key="2|intel-11th|16",
        spec_json=json.dumps({"generation": "2", "cpu_family": "intel-11th", "ram_tier": "16"}),
    )
    seed_observation(conn, "v1|real|0", 1_000_000, price_cents=20000, total_cents=20000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.find_deals()
    deal_ids = {d["item_id"] for d in result["deals"]}
    flagged_ids = {d["item_id"] for d in result["sanity_flagged"]}
    assert "v1|flagged|0" in flagged_ids
    assert "v1|flagged|0" not in deal_ids
    assert "v1|real|0" in deal_ids

    # Manual sabotage (source edit, run, revert - see report): appending
    # every scored entry to `deals` regardless of result.sanity_flagged
    # puts v1|flagged|0 at rank 1 of `deals` (lowest ratio_to_p25) - red-
    # confirmed and reverted.


def test_find_deals_excludes_incomplete_bucket_and_gone_listings(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|incomplete|0", spec_status="partial", bucket_key="2|?|16")
    seed_observation(conn, "v1|incomplete|0", 1_000_000, price_cents=10000, total_cents=10000)
    seed_listing(
        conn, "v1|gone|0", spec_status="ok", bucket_key="2|intel-11th|16",
        gone_at=2_000_000, lifespan_mins=60,
    )
    seed_observation(conn, "v1|gone|0", 1_000_000, price_cents=10000, total_cents=10000)
    seed_listing(
        conn, "v1|eligible|0", spec_status="ok", bucket_key="2|intel-11th|16",
        spec_json=json.dumps({"generation": "2", "cpu_family": "intel-11th", "ram_tier": "16"}),
    )
    seed_observation(conn, "v1|eligible|0", 1_000_000, price_cents=10000, total_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.find_deals()
    all_ids = {d["item_id"] for d in result["deals"] + result["sanity_flagged"]}
    assert all_ids == {"v1|eligible|0"}

    # Manual sabotage (source edit, run, revert - see report): dropping
    # `AND bucket_key NOT LIKE '%?%'` pulls v1|incomplete|0 in; dropping
    # `AND gone_at IS NULL` pulls v1|gone|0 in - each checked in turn,
    # red-confirmed and reverted.


def test_find_deals_rows_carry_baseline_layer_and_n(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    # No computed baseline exists for this bucket in a fresh db - this row
    # is necessarily seed-scored, the case design.md §15 tool 3 explicitly
    # calls out ("including for seed-scored rows").
    seed_listing(
        conn, "v1|seeded|0", spec_status="ok", bucket_key="1|intel-10th|16",
        spec_json=json.dumps({"generation": "1", "cpu_family": "intel-10th", "ram_tier": "16"}),
    )
    seed_observation(conn, "v1|seeded|0", 1_000_000, price_cents=10000, total_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.find_deals()
    assert len(result["deals"]) == 1
    deal = result["deals"][0]
    assert deal["baseline_layer"] == "seed"
    assert "baseline_n" in deal  # None for a seed row - the KEY still exists
    assert deal["baseline_n"] is None

    # Manual sabotage (source edit, run, revert - see report): omitting
    # baseline_layer/baseline_n from the entry dict makes the `in` checks
    # above fail with a KeyError - red-confirmed and reverted.


# --- 8/9: get_market_price -----------------------------------------------


def test_get_market_price_fast_candidate_count_matches_the_panel_function(tmp_path, monkeypatch):
    from dealwatch.engine.baselines import derive_candidates, group_fast_candidates_by_bucket

    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    bucket_key = "1|intel-10th|16"
    # Two fast (sweep-confirmed, dead within 24h) candidates in this
    # bucket, plus one dead-but-NEVER-SWEPT row (first_seen == last_seen)
    # that a naive COUNT(*) of dead listings in the bucket would still
    # count, but the real fast-candidate derivation excludes (V0.8b).
    for i in range(2):
        item_id = f"v1|fast{i}|0"
        seed_listing(
            conn, item_id, spec_status="ok", bucket_key=bucket_key,
            first_seen=1_000_000, last_seen=1_000_100,
            gone_at=1_000_100, lifespan_mins=1,
        )
        seed_observation(conn, item_id, 1_000_000, price_cents=15000, total_cents=15000)
    seed_listing(
        conn, "v1|neverswept|0", spec_status="ok", bucket_key=bucket_key,
        first_seen=1_000_000, last_seen=1_000_000, gone_at=1_000_000, lifespan_mins=None,
    )
    seed_observation(conn, "v1|neverswept|0", 1_000_000, price_cents=15000, total_cents=15000)
    conn.close()
    _use_db(monkeypatch, db_path)

    # The naive count a hand-rolled query might use - deliberately
    # different from the correct answer, so the fixture actually proves
    # something (L13: copy the module's own predicate, don't paraphrase it).
    naive_conn = sqlite3.connect(db_path)
    naive_count = naive_conn.execute(
        "SELECT COUNT(*) FROM listings WHERE bucket_key = ? AND gone_at IS NOT NULL "
        "AND spec_status = 'ok'",
        (bucket_key,),
    ).fetchone()[0]
    naive_conn.close()
    assert naive_count == 3  # includes the never-swept row - would be wrong

    result = mcp_server.get_market_price(generation="1", cpu_family="intel-10th", ram_tier="16")
    assert result["baseline"]["fast_candidates"] == 2
    assert result["baseline"]["fast_candidates"] != naive_count

    with_conn = connect(db_path)
    expected = len(
        group_fast_candidates_by_bucket(derive_candidates(with_conn), 24).get(bucket_key, [])
    )
    with_conn.close()
    assert result["baseline"]["fast_candidates"] == expected


def test_get_market_price_alert_time_series_is_gapped_not_interpolated(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    bucket_key = "1|intel-10th|16"
    day = 86400
    now = 20 * day
    # Alerts on 2 of the last 5 days for this bucket - the other 3 days
    # have nothing.
    seed_alert(
        conn, "v1|a1|0", sent_at=now - 4 * day, bucket_key=bucket_key,
        baseline_p25_cents=20000, baseline_layer="seed",
    )
    seed_alert(
        conn, "v1|a2|0", sent_at=now - 1 * day, bucket_key=bucket_key,
        baseline_p25_cents=19000, baseline_layer="computed",
    )
    conn.close()
    _use_db(monkeypatch, db_path)
    monkeypatch.setattr(mcp_server.time, "time", lambda: float(now))

    result = mcp_server.get_market_price(generation="1", cpu_family="intel-10th", ram_tier="16")
    series = result["alert_time_p25_series"]
    assert len(series) == 2  # not 5 - no forward-fill for the silent days
    assert [p["baseline_p25_cents"] for p in series] == [20000, 19000]

    # Manual sabotage (source edit, run, revert - see report): adding a
    # forward-fill pass that emits one entry per day in the 30-day window,
    # carrying the last known p25 forward across silent days, makes
    # len(series) come back 5 instead of 2 - red-confirmed and reverted.


# --- 10/11: get_alert_activity --------------------------------------------


def test_get_alert_activity_counts_events_not_rows(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|fanout|0", spec_status="ok", bucket_key="1|intel-10th|16")
    # One alert EVENT fanned out to two notifiers - same item_id, same
    # sent_at, two rows (the real shape since V0.9a).
    seed_alert(conn, "v1|fanout|0", sent_at=5_000_000, notifier="discord")
    seed_alert(conn, "v1|fanout|0", sent_at=5_000_000, notifier="pushover")
    conn.close()
    _use_db(monkeypatch, db_path)

    result = mcp_server.get_alert_activity(days=7)
    assert len(result["recent_events"]) == 1
    assert set(result["recent_events"][0]["delivery_statuses"]) == {"discord", "pushover"}


def test_get_alert_activity_sabotage_counting_raw_rows_instead_of_events(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|fanout|0", spec_status="ok", bucket_key="1|intel-10th|16")
    seed_alert(conn, "v1|fanout|0", sent_at=5_000_000, notifier="discord")
    seed_alert(conn, "v1|fanout|0", sent_at=5_000_000, notifier="pushover")
    conn.close()
    _use_db(monkeypatch, db_path)

    def _naive_rows(conn, profile_id, *, limit=20):
        rows = conn.execute(
            "SELECT item_id, sent_at, notifier, delivery_status FROM alerts "
            "WHERE profile_id = ? ORDER BY sent_at DESC LIMIT ?", (profile_id, limit),
        ).fetchall()
        return [{"item_id": r[0], "sent_at": r[1], "delivery_statuses": {r[2]: r[3]}} for r in rows]

    monkeypatch.setattr(mcp_server, "recent_alerts", _naive_rows)
    result = mcp_server.get_alert_activity(days=7)
    assert len(result["recent_events"]) == 2  # red: doubled, one row each


def test_get_alert_activity_live_and_dry_run_stay_split(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(conn, "v1|s|0", spec_status="ok", bucket_key="1|intel-10th|16")
    now = 10 * 86400
    for i in range(2):
        seed_alert(conn, "v1|s|0", sent_at=now - i * 100, dry_run=False)
    for i in range(3):
        seed_alert(conn, "v1|s|0", sent_at=now - i * 100 - 50, dry_run=True)
    conn.close()
    _use_db(monkeypatch, db_path)
    monkeypatch.setattr(mcp_server.time, "time", lambda: float(now))

    result = mcp_server.get_alert_activity(days=1)
    today = result["per_day_counts"][-1]
    assert today["count_live"] == 2
    assert today["count_dry"] == 3
    assert "count" not in today  # no merged total field

    # Manual sabotage (source edit, run, revert - see report): replacing
    # per_day_counts's pass-through with a version that sums count_live +
    # count_dry into one "count" field and drops the split loses exactly
    # the distinction this test checks - red-confirmed and reverted.


# --- 12/13: get_review_queue -----------------------------------------------


def test_get_review_queue_reuses_the_baseline_queue_panel(tmp_path, monkeypatch):
    from dealwatch.reporting.panels import baseline_queue as real_baseline_queue

    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|fast|0", spec_status="ok", bucket_key="1|intel-10th|16",
        first_seen=1_000_000, last_seen=1_000_100, gone_at=1_000_100, lifespan_mins=1,
    )
    seed_observation(conn, "v1|fast|0", 1_000_000, price_cents=15000, total_cents=15000)
    conn.close()
    _use_db(monkeypatch, db_path)

    with_conn = connect(db_path)
    expected = real_baseline_queue(
        with_conn, PROFILE_ID, min_samples=12, fast_lifespan_hours=24,
        compiled_seeds=mcp_server.compiled_seeds, limit=10,
    )
    with_conn.close()

    result = mcp_server.get_review_queue()
    assert result["baseline_queue"] == expected["queue"]
    assert result["negative_lifespan_dropped"] == expected["negative_lifespan_dropped"]


def test_get_review_queue_sabotage_a_different_baseline_queue_panel_output(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    fake_result = {
        "queue": [{"bucket_key": "9|fake|99", "fast_candidates": 99}],
        "negative_lifespan_dropped": 42,
    }
    monkeypatch.setattr(mcp_server, "baseline_queue", lambda *a, **k: fake_result)

    result = mcp_server.get_review_queue()
    # If get_review_queue independently recomputed its own ranking instead
    # of reusing panels.baseline_queue()'s output, it would disagree with
    # this stub - it doesn't, proving there is no second computation path.
    assert result["baseline_queue"] == fake_result["queue"]
    assert result["negative_lifespan_dropped"] == 42


def test_get_review_queue_drop_count_present_with_an_empty_queue(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    fake_result = {"queue": [], "negative_lifespan_dropped": 7}
    monkeypatch.setattr(mcp_server, "baseline_queue", lambda *a, **k: fake_result)

    result = mcp_server.get_review_queue()
    assert result["baseline_queue"] == []
    assert result["negative_lifespan_dropped"] == 7  # present and nonzero despite empty queue

    # Manual sabotage (source edit, run, revert - see report): nesting
    # "negative_lifespan_dropped" inside `if queue_result["queue"]:` before
    # building the return dict reproduces the exact V0.13 dashboard defect
    # shape - the key vanishes from the response precisely when the queue
    # is empty, which is the state a nonzero drop count matters most in.
    # Red-confirmed (KeyError / missing key) and reverted.


# ---------------------------------------------------------------------------
# 15. as_of/profile_id and the marketplace-caveat sentence, across all eight
# tools (design.md §15's "Conventions shared by every tool").
# ---------------------------------------------------------------------------


def test_every_tool_response_carries_as_of_and_profile_id_and_every_description_ends_with_the_caveat(
    tmp_path, monkeypatch
):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|cov|0", spec_status="ok", bucket_key="1|intel-10th|16",
        spec_json=json.dumps({"generation": "1", "cpu_family": "intel-10th", "ram_tier": "16"}),
    )
    seed_observation(conn, "v1|cov|0", 1_000_000, price_cents=10000, total_cents=10000)
    conn.close()
    _use_db(monkeypatch, db_path)

    calls = {
        "get_system_health": lambda: mcp_server.get_system_health(),
        "query_listings": lambda: mcp_server.query_listings(),
        "find_deals": lambda: mcp_server.find_deals(),
        "trace_title": lambda: mcp_server.trace_title(title="Lenovo ThinkPad T14 Gen 1 16GB 256GB"),
        "explain_listing": lambda: mcp_server.explain_listing(item_id_or_url="v1|cov|0"),
        "get_market_price": lambda: mcp_server.get_market_price(
            generation="1", cpu_family="intel-10th", ram_tier="16"
        ),
        "get_alert_activity": lambda: mcp_server.get_alert_activity(),
        "get_review_queue": lambda: mcp_server.get_review_queue(),
    }
    assert set(calls) == {t.name for t in mcp_server.mcp._tool_manager.list_tools()}

    for name, call in calls.items():
        result = call()
        assert "as_of" in result, f"{name} response missing as_of"
        assert result["profile_id"] == PROFILE_ID, f"{name} response missing/wrong profile_id"

    for tool in mcp_server.mcp._tool_manager.list_tools():
        assert tool.description.endswith(mcp_server._TOOL_CAVEAT), (
            f"{tool.name}'s description does not end with the marketplace-caveat sentence"
        )

    # Manual sabotage (source edit, run, revert - see report): stripping
    # the caveat sentence from get_review_queue's description makes the
    # endswith() check fail for exactly that tool - red-confirmed and
    # reverted.


# ---------------------------------------------------------------------------
# V1.0 prompt 2a - bucket vocabulary is a contract, not advice
# (design.md §15's dated 2a addendum). Tests numbered per
# v1.0-prompt-2a-vocabulary.md's own required-test list.
# ---------------------------------------------------------------------------


# --- 1/2: unknown values error, never fall through --------------------


def test_get_market_price_bad_cpu_family_errors_never_returns_seed(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    result = mcp_server.get_market_price(generation="1", cpu_family="Ryzen 5000", ram_tier="16")
    # V1.0 prompt 2b: observed-sourced wording says NOT OBSERVED, not
    # "unknown" - the check proved absence from collected data, not that
    # the profile can't produce it.
    assert result["error"] == "cpu_family 'Ryzen 5000' has not been observed in any collected listing"
    assert result["field"] == "cpu_family"
    assert "amd-ryzen-5000" in result["valid_values"]
    assert result["vocabulary_source"] == "observed"
    assert result["as_of"] > 0
    assert result["profile_id"] == PROFILE_ID
    assert "baseline" not in result
    assert "p25_cents" not in result

    # Manual sabotage (source edit, run, revert - see report): removing
    # get_market_price's three validation blocks in one pass and re-running
    # this test (and the generation/ram_tier tests below) together - every
    # one of them went red, with "Ryzen 5000" falling through to a real
    # seed price instead of an error. Reverted.


def test_get_market_price_bad_generation_errors_never_returns_seed(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    result = mcp_server.get_market_price(generation="Gen 2", cpu_family="amd-ryzen-5000", ram_tier="16")
    assert result["error"] == "unknown generation 'Gen 2'"
    assert result["field"] == "generation"
    assert result["valid_values"] == ["1", "2", "3", "4", "5", "6"]
    assert result["vocabulary_source"] == "profile"
    assert "baseline" not in result


def test_get_market_price_bad_ram_tier_errors_never_returns_seed(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    result = mcp_server.get_market_price(generation="1", cpu_family="amd-ryzen-5000", ram_tier="16GB")
    assert result["error"] == "unknown ram_tier '16GB'"
    assert result["field"] == "ram_tier"
    assert set(result["valid_values"]) == {"8", "16", "32", "48"}
    assert result["vocabulary_source"] == "profile"
    assert "baseline" not in result


def test_query_listings_bad_bucket_filters_error_instead_of_zero_matches(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|z1|0", spec_status="ok", bucket_key="2|amd-ryzen-5000|16",
        spec_json=json.dumps({"generation": "2", "cpu_family": "amd-ryzen-5000", "ram_tier": "16"}),
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    for kwargs, field in (
        ({"generation": "Gen 2"}, "generation"),
        ({"cpu_family": "Ryzen 5000"}, "cpu_family"),
        ({"ram_tier": "16GB"}, "ram_tier"),
    ):
        result = mcp_server.query_listings(**kwargs)
        assert result["field"] == field
        assert "error" in result
        assert "total_count" not in result  # never silently ran the query

    # Manual sabotage (source edit, run, revert - see report): removing
    # query_listings' three validation blocks in one pass - re-running
    # this test showed all three cases silently returning total_count: 0
    # (or, for the real value some rows still matched) instead of an
    # error - red-confirmed, reverted.


# --- 3: valid_values tracks the profile, not a hardcoded copy ----------


def test_valid_values_matches_the_profile_vocabulary_helper_not_a_hardcoded_list(
    tmp_path, monkeypatch
):
    from dealwatch.mcp_server.vocabulary import build_bucket_vocabulary

    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    # Independently recomputed from the same real profile this server
    # loaded at import time - not read back from mcp_server's own cache.
    fresh = build_bucket_vocabulary(mcp_server.profile, None)

    result = mcp_server.get_market_price(generation="9", cpu_family="amd-ryzen-5000", ram_tier="16")
    assert result["field"] == "generation"
    assert result["valid_values"] == fresh["generation"]["values"]

    # Manual sabotage (source edit, run, revert - see report):
    # _validate_bucket_field()'s `vocabulary = _current_bucket_vocabulary()
    # [field]` line replaced with a hardcoded stale dict for "generation"
    # ({"values": ["1", "2"], "source": "profile"} - missing 3-6) -
    # result["valid_values"] no longer equals fresh["generation"]["values"]
    # (["1",...,"6"]) - red-confirmed, reverted.


# --- 4: description vocabulary matches the helper's output -------------


def test_tool_descriptions_embed_the_real_vocabulary_not_a_hardcoded_copy():
    tools = {t.name: t for t in mcp_server.mcp._tool_manager.list_tools()}
    for name in ("get_market_price", "query_listings"):
        description = tools[name].description
        for field in ("generation", "ram_tier"):
            expected = repr(mcp_server._DESCRIPTION_VOCABULARY_SNAPSHOT[field]["values"])
            assert expected in description, f"{name} description missing {field}'s real values"

    # Manual sabotage (source edit, run, revert - see report): replacing
    # query_listings' embedded generation list with a hardcoded literal
    # string ("['1', '2']") instead of the repr(...) expression - this
    # test failed on the missing "['1', '2', '3', '4', '5', '6']"
    # substring - red-confirmed, reverted.


# --- 5: case-insensitive exact match, no fuzzy matching -----------------


def test_case_insensitive_match_accepted_near_miss_rejected(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    ok = mcp_server.get_market_price(generation="2", cpu_family="AMD-RYZEN-5000", ram_tier="16")
    assert "error" not in ok
    assert ok["resolved_bucket_key"] == "2|amd-ryzen-5000|16"  # canonical casing, not the caller's

    bad = mcp_server.get_market_price(generation="2", cpu_family="Ryzen 5000", ram_tier="16")
    assert bad.get("error") == "cpu_family 'Ryzen 5000' has not been observed in any collected listing"

    # Manual sabotage (source edit, run, revert - see report): added a
    # difflib.get_close_matches() fallback to _validate_bucket_field() so
    # a near-miss resolves to its closest vocabulary entry instead of
    # erroring - "Ryzen 5000" then resolved to "amd-ryzen-5000" with no
    # error, exactly the class of bug this prompt exists to close -
    # red-confirmed, reverted.


# --- 6: resolved_bucket_key everywhere a bucket key is used -------------


def test_resolved_bucket_key_present_and_correct_everywhere(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|rbk|0", spec_status="ok", bucket_key="2|amd-ryzen-5000|16",
        spec_json=json.dumps({"generation": "2", "cpu_family": "amd-ryzen-5000", "ram_tier": "16"}),
    )
    seed_observation(conn, "v1|rbk|0", 1_000_000, price_cents=20000, total_cents=20000)
    conn.close()
    _use_db(monkeypatch, db_path)

    price_result = mcp_server.get_market_price(generation="2", cpu_family="amd-ryzen-5000", ram_tier="16")
    assert price_result["resolved_bucket_key"] == "2|amd-ryzen-5000|16"

    deals_result = mcp_server.find_deals()
    assert len(deals_result["deals"]) == 1
    assert deals_result["deals"][0]["resolved_bucket_key"] == "2|amd-ryzen-5000|16"

    explain_result = mcp_server.explain_listing(item_id_or_url="v1|rbk|0")
    assert explain_result["resolved_bucket_key"] == "2|amd-ryzen-5000|16"

    # Manual sabotage (source edit, run, revert - see report): omitted
    # "resolved_bucket_key" from explain_listing's top-level return dict -
    # this test's explain_result["resolved_bucket_key"] access raised
    # KeyError - red-confirmed, reverted.


# --- 7: the two empty cases are distinguishable --------------------------


def test_get_market_price_distinguishes_no_baseline_from_never_observed(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    # Fixture A: dead listings exist in this bucket, but fewer than
    # min_samples (12) - "no_computed_baseline".
    for i in range(3):
        item_id = f"v1|dead{i}|0"
        seed_listing(
            conn, item_id, spec_status="ok", bucket_key="1|intel-10th|16",
            first_seen=1_000_000, last_seen=1_000_100, gone_at=1_000_100, lifespan_mins=1,
        )
        seed_observation(conn, item_id, 1_000_000, price_cents=15000, total_cents=15000)
    conn.close()
    _use_db(monkeypatch, db_path)

    observed = mcp_server.get_market_price(generation="1", cpu_family="intel-10th", ram_tier="16")
    assert observed["baseline"]["state"] == "no_computed_baseline"
    assert observed["baseline"]["fast_candidates"] == 3

    # Fixture B: nothing has ever normalized into this bucket at all.
    never = mcp_server.get_market_price(generation="4", cpu_family="intel-12th", ram_tier="8")
    assert never["baseline"]["state"] == "bucket_never_observed"
    assert never["baseline"]["fast_candidates"] == 0

    assert observed["baseline"]["state"] != never["baseline"]["state"]

    # Manual sabotage (source edit, run, revert - see report): hardcoded
    # `ever_observed = True` regardless of the query - both fixtures
    # reported "no_computed_baseline", collapsing the distinction this
    # test exists to check - red-confirmed, reverted.


# --- 8: over the wire, an argument error is isError: false --------------


def test_invalid_bucket_argument_over_http_is_data_not_a_protocol_error(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    with TestClient(mcp_server._build_app()) as client:
        response = _call_over_http(
            client, "get_market_price",
            {"generation": "Gen 2", "cpu_family": "amd-ryzen-5000", "ram_tier": "16"},
            request_id=1,
        )
        assert response.status_code == 200
        result = response.json()["result"]
        assert result["isError"] is False
        payload = json.loads(result["content"][0]["text"])
        assert payload["error"] == "unknown generation 'Gen 2'"
        assert payload["as_of"] > 0
        assert payload["profile_id"] == PROFILE_ID


# ---------------------------------------------------------------------------
# V1.0 prompt 2b - vocabulary freshness and visibility (design.md §15's
# dated 2b addendum). Tests numbered per
# v1.0-prompt-2b-vocabulary-freshness.md's own required-test list.
# ---------------------------------------------------------------------------


# --- 1: the degraded path is visible and survives import ----------------


def test_degraded_vocabulary_path_imports_and_serves(tmp_path, monkeypatch):
    # Opt out of the autouse cpu_family seed - this test exercises the
    # real degraded/empty state, which the seed exists precisely to hide
    # from every other test in this file.
    mcp_server._observed_vocabulary_cache.pop("cpu_family", None)

    # Import survival: a genuinely unreadable database (never created) at
    # import time must not crash the module - the try/except inside
    # _current_bucket_vocabulary() (exercised once, at import, for
    # _DESCRIPTION_VOCABULARY_SNAPSHOT) is what prevents that.
    never_created = tmp_path / "never-created.db"
    script = (
        "import os\n"
        f"os.environ['PROFILE_PATH'] = {PROFILE_PATH!r}\n"
        f"os.environ['DB_PATH'] = {str(never_created)!r}\n"
        "import dealwatch.mcp_server.server\n"
        "print('IMPORT_OK')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, cwd=str(REPO_ROOT)
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "IMPORT_OK" in result.stdout

    # Serves degraded: a READABLE but listings-less database (migrated,
    # zero rows) - /health reports the zero count and the "degraded"
    # status item 1's decision picked (200, not 503 - a narrow,
    # self-healing degradation, not a whole-server outage - design.md's
    # 2b addendum has the reasoning); a cpu_family argument is rejected as
    # structured data, never an exception.
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    with TestClient(mcp_server._build_app()) as client:
        health_response = client.get("/health")
    assert health_response.status_code == 200
    health_body = health_response.json()
    assert health_body["vocabulary"]["cpu_family"]["count"] == 0
    assert health_body["vocabulary_degraded"] is True
    assert health_body["status"] == "degraded"

    result = mcp_server.get_market_price(generation="1", cpu_family="amd-ryzen-5000", ram_tier="16")
    assert "error" in result
    assert result["field"] == "cpu_family"

    # Manual sabotage (source edit, run, revert - see report): removed the
    # try/except inside _current_bucket_vocabulary() around the
    # fetch_observed_vocabularies() call - the subprocess import above
    # then raised sqlite3.OperationalError and the import itself failed
    # (returncode != 0, "IMPORT_OK" never printed) - red-confirmed and
    # reverted.


# --- 2: an empty/failed build is never cached ----------------------------


def test_empty_build_is_not_cached_retries_on_next_call(tmp_path, monkeypatch):
    mcp_server._observed_vocabulary_cache.pop("cpu_family", None)

    # First call: database doesn't exist yet at all - the fetch fails,
    # degrades, and (this is the point) is NOT cached.
    unreadable = tmp_path / "not-yet.db"
    _use_db(monkeypatch, unreadable)
    first = mcp_server.get_market_price(generation="1", cpu_family="amd-ryzen-5000", ram_tier="16")
    assert first.get("field") == "cpu_family"
    assert "cpu_family" not in mcp_server._observed_vocabulary_cache

    # Database becomes available, with the value present - no restart.
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|x|0", spec_status="ok", bucket_key="1|amd-ryzen-5000|16",
        spec_json=json.dumps({"generation": "1", "cpu_family": "amd-ryzen-5000", "ram_tier": "16"}),
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    second = mcp_server.get_market_price(generation="1", cpu_family="amd-ryzen-5000", ram_tier="16")
    assert "error" not in second

    # Manual sabotage (source edit, run, revert - see report): cached the
    # empty fetch result unconditionally (dropped the `if values:` guard
    # before writing to _observed_vocabulary_cache) - the second call,
    # even with the database now readable and the value present, still
    # returned the cached empty vocabulary and rejected the value -
    # red-confirmed and reverted.


# --- 3: a successful build IS cached within the TTL -----------------------


def test_successful_build_is_cached_within_ttl(tmp_path, monkeypatch):
    mcp_server._observed_vocabulary_cache.pop("cpu_family", None)
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|x|0", spec_status="ok", bucket_key="1|amd-ryzen-5000|16",
        spec_json=json.dumps({"generation": "1", "cpu_family": "amd-ryzen-5000", "ram_tier": "16"}),
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    calls = []
    real_fetch = mcp_server.fetch_observed_vocabularies

    def _counting_fetch(conn, profile_id, fields):
        calls.append(list(fields))
        return real_fetch(conn, profile_id, fields)

    monkeypatch.setattr(mcp_server, "fetch_observed_vocabularies", _counting_fetch)

    mcp_server.get_market_price(generation="1", cpu_family="amd-ryzen-5000", ram_tier="16")
    mcp_server.get_market_price(generation="1", cpu_family="amd-ryzen-5000", ram_tier="16")
    assert len(calls) == 1

    # Manual sabotage (source edit, run, revert - see report): the
    # `stale_or_missing` filter in _current_bucket_vocabulary() changed to
    # always include every observed field regardless of the cache -
    # `calls` came back length 2 instead of 1 - red-confirmed and reverted.


# --- 4: a newly-observed value is accepted after the TTL ------------------


def test_newly_observed_value_accepted_after_ttl_expiry(tmp_path, monkeypatch):
    mcp_server._observed_vocabulary_cache.pop("cpu_family", None)
    db_path = _make_db(tmp_path)
    conn = connect(db_path)
    seed_listing(
        conn, "v1|x|0", spec_status="ok", bucket_key="1|amd-ryzen-5000|16",
        spec_json=json.dumps({"generation": "1", "cpu_family": "amd-ryzen-5000", "ram_tier": "16"}),
    )
    conn.close()
    _use_db(monkeypatch, db_path)

    clock = [1_000_000.0]
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: clock[0])

    # amd-ryzen-7000 not present at build time.
    first = mcp_server.get_market_price(generation="4", cpu_family="amd-ryzen-7000", ram_tier="16")
    assert first.get("field") == "cpu_family"

    # A real listing lands.
    conn = connect(db_path)
    seed_listing(
        conn, "v1|y|0", spec_status="ok", bucket_key="4|amd-ryzen-7000|16",
        spec_json=json.dumps({"generation": "4", "cpu_family": "amd-ryzen-7000", "ram_tier": "16"}),
    )
    conn.close()

    # Still inside the TTL - still rejected (serving the cached build).
    clock[0] += 30
    still_rejected = mcp_server.get_market_price(generation="4", cpu_family="amd-ryzen-7000", ram_tier="16")
    assert still_rejected.get("field") == "cpu_family"

    # Past the TTL - the rebuild picks up the newly-observed value, no
    # restart needed. Clock controlled explicitly, never slept on.
    clock[0] += mcp_server._VOCABULARY_TTL_SECONDS
    accepted = mcp_server.get_market_price(generation="4", cpu_family="amd-ryzen-7000", ram_tier="16")
    assert "error" not in accepted

    # Manual sabotage (source edit, run, revert - see report): pinned
    # _current_bucket_vocabulary() to always treat "cpu_family" as fresh
    # (skip the TTL-elapsed check entirely) - the post-expiry call still
    # rejected amd-ryzen-7000 - red-confirmed and reverted.


# --- 5: profile-sourced fields are never rebuilt on the TTL path ----------


def test_profile_sourced_fields_are_not_rebuilt_on_ttl_path(monkeypatch):
    calls = []
    real_resolve = mcp_server.resolve_profile_vocabulary

    def _counting_resolve(profile):
        calls.append(profile)
        return real_resolve(profile)

    monkeypatch.setattr(mcp_server, "resolve_profile_vocabulary", _counting_resolve)

    clock = [2_000_000.0]
    monkeypatch.setattr(mcp_server.time, "monotonic", lambda: clock[0])

    mcp_server._current_bucket_vocabulary()
    clock[0] += mcp_server._VOCABULARY_TTL_SECONDS * 3
    mcp_server._current_bucket_vocabulary()
    mcp_server._current_bucket_vocabulary()

    # resolve_profile_vocabulary() ran exactly once, at import - never
    # again, regardless of how many times the TTL has elapsed since.
    assert calls == []

    # Manual sabotage (source edit, run, revert - see report):
    # _current_bucket_vocabulary() changed to call
    # resolve_profile_vocabulary(profile) fresh on every invocation
    # instead of reading the fixed _profile_vocabulary - `calls` came back
    # non-empty - red-confirmed and reverted.


# --- 6: /health's vocabulary block matches the live accessor --------------


def test_health_vocabulary_block_matches_the_live_accessor(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    expected = mcp_server._current_bucket_vocabulary()

    with TestClient(mcp_server._build_app()) as client:
        response = client.get("/health")
    body = response.json()

    for field in ("generation", "cpu_family", "ram_tier"):
        assert body["vocabulary"][field]["count"] == len(expected[field]["values"]), field
        assert body["vocabulary"][field]["source"] == expected[field]["source"], field

    # Manual sabotage (source edit, run, revert - see report):
    # _vocabulary_health_block() hardcoded "count": 999 for every field
    # instead of len(vocabulary[field]["values"]) - every one of the three
    # assertions above failed - red-confirmed and reverted.


# --- 7: rejection wording differs by vocabulary source ---------------------


def test_rejection_wording_differs_by_vocabulary_source(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    observed_error = mcp_server.get_market_price(generation="1", cpu_family="nonsense", ram_tier="16")
    assert observed_error["error"] == "cpu_family 'nonsense' has not been observed in any collected listing"
    assert "unknown" not in observed_error["error"]

    profile_error = mcp_server.get_market_price(generation="99", cpu_family="amd-ryzen-5000", ram_tier="16")
    assert profile_error["error"] == "unknown generation '99'"
    assert "observed" not in profile_error["error"]

    # Manual sabotage (source edit, run, revert - see report):
    # _validate_bucket_field() changed to always build the "unknown
    # {field} {value!r}" message regardless of vocabulary["source"] -
    # observed_error["error"] became "unknown cpu_family 'nonsense'",
    # failing the not-observed assertion above - red-confirmed and
    # reverted.


# --- 8: find_deals validates its bucket arguments --------------------------


def test_find_deals_rejects_bad_bucket_argument_instead_of_empty_deals(tmp_path, monkeypatch):
    db_path = _make_db(tmp_path)
    _use_db(monkeypatch, db_path)

    result = mcp_server.find_deals(cpu_family="Ryzen 5000")
    assert "deals" not in result
    assert result.get("field") == "cpu_family"
    assert result.get("as_of") is not None
    assert result.get("profile_id") == PROFILE_ID

    # Manual sabotage (source edit, run, revert - see report): removed
    # find_deals' three validation blocks - "Ryzen 5000" then silently
    # matched zero listings and the response carried "deals": [] (a
    # legitimate-looking, empty market answer) instead of an error -
    # red-confirmed and reverted.
