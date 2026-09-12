"""Tests for dealwatch.reporting.panels (V0.11, design.md §13).

Same seeding approach as tests/test_status.py: real SQLite via the real
connect() (so migration 8's indexes exist), hand-seeded rows directly
against listings/observations/alerts/baselines - panels.py is a pure read
layer, no reason to run listings through the collector or scoring to test
it.
"""

from datetime import datetime

from dealwatch.providers.ratelimit import PACIFIC
from dealwatch.reporting import panels
from dealwatch.storage.sqlite import connect

PROFILE = "thinkpad-t14"


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
    item_web_url=None,
):
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "bucket_key, first_seen, last_seen, miss_count, gone_at, lifespan_mins, "
        "item_web_url) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)",
        (item_id, profile_id, title, spec_status, bucket_key, first_seen,
         last_seen, gone_at, lifespan_mins, item_web_url),
    )


def seed_observation(conn, item_id, observed_at, *, price_cents=None, total_cents=None):
    conn.execute(
        "INSERT INTO observations (item_id, observed_at, price_cents, total_cents, raw_json) "
        "VALUES (?, ?, ?, ?, '{}')",
        (item_id, observed_at, price_cents, total_cents),
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
    price_cents=9000,
):
    conn.execute(
        "INSERT INTO alerts (item_id, profile_id, sent_at, dry_run, price_cents, "
        "price_is_price_only, bucket_key, baseline_layer, baseline_match, baseline_n, "
        "baseline_p25_cents, baseline_p50_cents, ratio_to_p25, sanity_flagged, "
        "delivery_status, notifier) VALUES (?, ?, ?, ?, ?, 0, '1|intel-10th|16', "
        "?, '{}', NULL, 10000, 15000, ?, 0, ?, ?)",
        (item_id, profile_id, sent_at, 1 if dry_run else 0, price_cents,
         baseline_layer, ratio_to_p25, delivery_status, notifier),
    )


def seed_baseline(conn, *, profile_id=PROFILE, bucket_key="1|intel-10th|16", computed_at=1000):
    conn.execute(
        "INSERT INTO baselines (profile_id, bucket_key, n, n_price_only, p10_cents, "
        "p25_cents, p50_cents, fast_hours, computed_at) VALUES (?, ?, 12, 0, 9000, "
        "10000, 15000, 24, ?)",
        (profile_id, bucket_key, computed_at),
    )


_NOON = int(datetime(2026, 9, 10, 12, 0, tzinfo=PACIFIC).timestamp())
_TODAY_START = int(datetime(2026, 9, 10, 0, 0, tzinfo=PACIFIC).timestamp())


# ---------------------------------------------------------------------------
# alerts_per_day
# ---------------------------------------------------------------------------


def test_alerts_per_day_counts_events_not_rows(tmp_path):
    # Breaking mechanism: a bare COUNT(*) instead of event_count()'s
    # distinct (item_id, sent_at) dedup would report 2, not 1.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1")
    seed_alert(conn, "item-1", sent_at=_TODAY_START + 100, notifier="discord")
    seed_alert(conn, "item-1", sent_at=_TODAY_START + 100, notifier="pushover")

    entries = panels.alerts_per_day(conn, PROFILE, days=1, now=_NOON)

    assert len(entries) == 1
    assert entries[0]["day_start"] == _TODAY_START
    assert entries[0]["count"] == 1


def test_alerts_per_day_is_oldest_first_with_no_gaps(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1")
    seed_alert(conn, "item-1", sent_at=_TODAY_START + 100)

    entries = panels.alerts_per_day(conn, PROFILE, days=14, now=_NOON)

    assert len(entries) == 14
    day_starts = [e["day_start"] for e in entries]
    assert day_starts == sorted(day_starts)
    for earlier, later in zip(day_starts, day_starts[1:]):
        gap_hours = (later - earlier) / 3600
        assert 23 <= gap_hours <= 25  # 24h normally, 23/25 across a DST boundary
    assert entries[-1]["day_start"] == _TODAY_START
    assert entries[-1]["count"] == 1


def test_alerts_per_day_spans_the_fall_back_dst_transition(tmp_path):
    # 2026-11-01: US DST ends - the LA calendar day of 2026-11-01 is 25
    # hours long (90000 seconds; every other day here is 86400). `now` is
    # set a few days AFTER the transition so the 14-day walk-back has to
    # cross Nov 1 as an INTERMEDIATE step, not as the starting day -
    # walking backward from Nov 1 itself (or earlier) never exposes a
    # fixed-86400 bug, because Oct 31 (the day immediately before the
    # transition) is an ordinary 24-hour day and 86400 gets that one step
    # right by coincidence. The bug only shows up once the walk has to
    # account for Nov 1's own 25-hour length to land on its true start.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1")
    nov_1_start = int(datetime(2026, 11, 1, 0, 0, tzinfo=PACIFIC).timestamp())
    now = int(datetime(2026, 11, 4, 12, 0, tzinfo=PACIFIC).timestamp())
    # In the repeated 1am-2am hour - only ever correctly bucketed into
    # Nov 1's day if day boundaries come from la_day_bounds(), not a fixed
    # 24-hour guess.
    seed_alert(conn, "item-1", sent_at=nov_1_start + 24 * 3600 + 60)

    entries = panels.alerts_per_day(conn, PROFILE, days=14, now=now)

    assert len(entries) == 14
    day_starts = [e["day_start"] for e in entries]
    assert len(set(day_starts)) == 14  # every day distinct - none skipped or duplicated
    assert nov_1_start in day_starts
    nov_1_entry = next(e for e in entries if e["day_start"] == nov_1_start)
    assert nov_1_entry["count"] == 1


# ---------------------------------------------------------------------------
# recent_alerts
# ---------------------------------------------------------------------------


def test_recent_alerts_fan_out_counts_as_one_event_not_two_slots(tmp_path):
    # 25 distinct alert events, each fanned out to both notifiers (50 rows
    # total) - a limit of 20 must return 20 distinct EVENTS, not 10.
    conn = make_conn(tmp_path)
    for i in range(25):
        item_id = f"item-{i}"
        seed_listing(conn, item_id, title=f"title {i}", first_seen=1000 + i)
        seed_alert(conn, item_id, sent_at=2000 + i, notifier="discord")
        seed_alert(conn, item_id, sent_at=2000 + i, notifier="pushover")

    results = panels.recent_alerts(conn, PROFILE, limit=20)

    assert len(results) == 20
    assert len({(r["item_id"], r["sent_at"]) for r in results}) == 20
    assert [r["sent_at"] for r in results] == sorted(
        (r["sent_at"] for r in results), reverse=True
    )
    assert results[0]["delivery_statuses"] == {"discord": "sent", "pushover": "sent"}


def test_recent_alerts_includes_listing_title_and_url(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", title="ThinkPad T14 Gen 2", item_web_url="https://example.com/1")
    seed_alert(conn, "item-1", sent_at=2000, ratio_to_p25=0.72, baseline_layer="computed")

    results = panels.recent_alerts(conn, PROFILE, limit=20)

    assert len(results) == 1
    r = results[0]
    assert r["title"] == "ThinkPad T14 Gen 2"
    assert r["item_web_url"] == "https://example.com/1"
    assert r["ratio_to_p25"] == 0.72
    assert r["baseline_layer"] == "computed"
    assert r["dry_run"] is False
    assert r["delivery_statuses"] == {"discord": "sent"}


def test_recent_alerts_respects_profile_isolation(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "mine")
    seed_listing(conn, "theirs", profile_id="other-profile")
    seed_alert(conn, "mine", sent_at=2000)
    seed_alert(conn, "theirs", profile_id="other-profile", sent_at=2000)

    results = panels.recent_alerts(conn, PROFILE, limit=20)

    assert len(results) == 1
    assert results[0]["item_id"] == "mine"


# ---------------------------------------------------------------------------
# recent_listings
# ---------------------------------------------------------------------------


def test_recent_listings_orders_by_first_seen_desc_and_respects_limit(tmp_path):
    conn = make_conn(tmp_path)
    for i in range(5):
        seed_listing(conn, f"item-{i}", first_seen=1000 + i, title=f"t{i}")

    results = panels.recent_listings(conn, PROFILE, limit=3)

    assert [r["item_id"] for r in results] == ["item-4", "item-3", "item-2"]


def test_recent_listings_price_prefers_total_cents_over_price_cents(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", first_seen=1000)
    seed_observation(conn, "item-1", 1000, price_cents=9000, total_cents=None)
    seed_observation(conn, "item-1", 1100, price_cents=9500, total_cents=10500)

    results = panels.recent_listings(conn, PROFILE, limit=10)

    assert results[0]["price_cents"] == 10500  # latest observation; total_cents wins


def test_recent_listings_reports_active_flag(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "alive", first_seen=1000, gone_at=None)
    seed_listing(conn, "dead", first_seen=1100, gone_at=2000)

    results = panels.recent_listings(conn, PROFILE, limit=10)

    by_id = {r["item_id"]: r for r in results}
    assert by_id["alive"]["active"] is True
    assert by_id["dead"]["active"] is False


# ---------------------------------------------------------------------------
# baseline_coverage
# ---------------------------------------------------------------------------


def test_baseline_coverage_excludes_incomplete_and_null_bucket_keys(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "complete-1", bucket_key="1|intel-10th|16", first_seen=1000)
    seed_listing(conn, "complete-2", bucket_key="2|intel-11th|16", first_seen=1001)
    seed_listing(conn, "incomplete", bucket_key="1|?|16", first_seen=1002)
    seed_listing(conn, "no-bucket", bucket_key=None, first_seen=1003)
    seed_baseline(conn, bucket_key="1|intel-10th|16")

    coverage = panels.baseline_coverage(conn, PROFILE)

    assert coverage["buckets_observed"] == 2
    assert coverage["buckets_with_baseline"] == 1
    assert coverage["coverage_pct"] == 0.5


def test_baseline_coverage_excludes_dead_listings(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "dead", bucket_key="1|intel-10th|16", first_seen=1000, gone_at=2000)

    coverage = panels.baseline_coverage(conn, PROFILE)

    assert coverage["buckets_observed"] == 0
    assert coverage["coverage_pct"] is None  # not a ZeroDivisionError, not 0.0
