"""Tests for dealwatch.reporting.panels (V0.11, design.md §13).

Same seeding approach as tests/test_status.py: real SQLite via the real
connect() (so migration 8's indexes exist), hand-seeded rows directly
against listings/observations/alerts/baselines - panels.py is a pure read
layer, no reason to run listings through the collector or scoring to test
it.
"""

import json
from datetime import datetime

import pytest

from dealwatch.engine.scoring import compile_seed_baselines
from dealwatch.normalize.schema import PollConfig, Profile, SearchConfig
from dealwatch.providers.ratelimit import PACIFIC
from dealwatch.reporting import panels
from dealwatch.storage.sqlite import connect

PROFILE = "thinkpad-t14"


def make_seeds(seed_baselines):
    """compile_seed_baselines() needs a Profile, but panels.py's
    baseline_queue() only ever sees its already-compiled output
    (V0.12 Part B2) - this builds just enough of a Profile to compile a
    given seed_baselines list, matching test_scoring.py's make_profile()
    shape."""
    profile = Profile(
        id=PROFILE,
        name="Test Profile",
        search=SearchConfig(queries=["q"], filters={}, poll=PollConfig()),
        scoring={},
        seed_baselines=seed_baselines,
    )
    return compile_seed_baselines(profile)


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
    spec_json=None,
):
    conn.execute(
        "INSERT INTO listings (item_id, profile_id, title, spec_status, "
        "bucket_key, first_seen, last_seen, miss_count, gone_at, lifespan_mins, "
        "item_web_url, spec_json) VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
        (item_id, profile_id, title, spec_status, bucket_key, first_seen,
         last_seen, gone_at, lifespan_mins, item_web_url, spec_json),
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
    assert entries[0]["count_live"] == 1


def test_alerts_per_day_splits_live_from_dry_run(tmp_path):
    # A1: a mode-merged count would make a dry-run calibration day read as
    # a real spike - live and dry must land in separate fields.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1")
    seed_listing(conn, "item-2")
    seed_alert(conn, "item-1", sent_at=_TODAY_START + 100, dry_run=False)
    seed_alert(conn, "item-2", sent_at=_TODAY_START + 200, dry_run=True)
    seed_alert(conn, "item-2", sent_at=_TODAY_START + 300, dry_run=True)

    entries = panels.alerts_per_day(conn, PROFILE, days=1, now=_NOON)

    assert entries[0]["count_live"] == 1
    assert entries[0]["count_dry"] == 2


def test_alerts_per_day_label_is_the_short_calendar_date(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1")

    entries = panels.alerts_per_day(conn, PROFILE, days=1, now=_NOON)

    assert entries[0]["label"] == "Sep 10"


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
    assert entries[-1]["count_live"] == 1


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
    assert nov_1_entry["count_live"] == 1


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
    assert r["price_display"] == "$90.00"  # A6: seed_alert()'s default price_cents=9000
    assert r["sent_at_display"] != "unknown"


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
    assert results[0]["price_display"] == "$105.00"
    assert results[0]["first_seen_display"] != "unknown"


def test_recent_listings_price_display_is_unknown_with_no_observation(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", first_seen=1000)

    results = panels.recent_listings(conn, PROFILE, limit=10)

    assert results[0]["price_cents"] is None
    assert results[0]["price_display"] == "unknown"


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
    assert coverage["coverage_fraction"] == 0.5
    assert coverage["coverage_display"] == "50%"


def test_baseline_coverage_excludes_dead_listings(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "dead", bucket_key="1|intel-10th|16", first_seen=1000, gone_at=2000)

    coverage = panels.baseline_coverage(conn, PROFILE)

    assert coverage["buckets_observed"] == 0
    assert coverage["coverage_fraction"] is None  # not a ZeroDivisionError, not 0.0
    assert coverage["coverage_display"] == "unknown"


# ---------------------------------------------------------------------------
# computed_baselines (V0.11a Part D)
# ---------------------------------------------------------------------------


def test_computed_baselines_returns_display_strings_and_n_ordered_by_bucket_key(tmp_path):
    conn = make_conn(tmp_path)
    seed_baseline(conn, bucket_key="2|intel-11th|16", computed_at=2000)
    conn.execute(
        "UPDATE baselines SET n = 24, n_price_only = 3, fast_hours = 24 "
        "WHERE bucket_key = '2|intel-11th|16'"
    )
    conn.execute(
        "INSERT INTO baselines (profile_id, bucket_key, n, n_price_only, p10_cents, "
        "p25_cents, p50_cents, fast_hours, computed_at) VALUES (?, '1|intel-10th|16', "
        "14, 1, 16000, 16499, 19550, 24, ?)",
        (PROFILE, 1000),
    )

    results = panels.computed_baselines(conn, PROFILE)

    assert [r["bucket_key"] for r in results] == ["1|intel-10th|16", "2|intel-11th|16"]

    first = results[0]
    assert first["n"] == 14
    assert first["n_price_only"] == 1
    assert first["fast_hours"] == 24
    assert first["p10_display"] == "$160.00"
    assert first["p25_display"] == "$164.99"
    assert first["p50_display"] == "$195.50"
    assert first["computed_at_display"] != "unknown"

    second = results[1]
    assert second["n"] == 24
    assert second["p25_display"] == "$100.00"  # seed_baseline()'s own default p25_cents=10000


def test_computed_baselines_is_profile_scoped(tmp_path):
    conn = make_conn(tmp_path)
    seed_baseline(conn, profile_id="other-profile", bucket_key="1|intel-10th|16")

    results = panels.computed_baselines(conn, PROFILE)

    assert results == []


# ---------------------------------------------------------------------------
# baseline_queue (V0.11a Part E)
# ---------------------------------------------------------------------------


def _seed_candidate(
    conn, item_id, bucket_key, *, first_seen, price_cents=20000, lifespan_seconds=600, spec=None
):
    """A listing shaped exactly like one derive_candidates() would accept:
    dead, sweep-confirmed (first_seen != last_seen), spec_status='ok', no
    variation_id, a complete bucket_key, and a priced observation.
    lifespan_seconds defaults to 600 (10 min) - fast under any realistic
    fast_lifespan_hours threshold; pass a much larger value to seed a
    SLOW candidate (V0.11b Part A's ranking fix needs both shapes).
    spec (V0.12 Part B) is serialized into spec_json - baseline_queue()
    reads it back via the real parse_spec_json() to resolve a seed;
    defaults to {} (an empty spec, matching a listing whose fields never
    parsed) when not given."""
    seed_listing(
        conn, item_id, bucket_key=bucket_key, first_seen=first_seen,
        last_seen=first_seen + lifespan_seconds // 2, gone_at=first_seen + lifespan_seconds,
        spec_json=json.dumps(spec if spec is not None else {}),
    )
    seed_observation(
        conn, item_id, first_seen + lifespan_seconds // 2, price_cents=price_cents
    )


def test_baseline_queue_excludes_computed_buckets_and_orders_by_fast_candidate_count(tmp_path):
    conn = make_conn(tmp_path)
    conn.row_factory = None  # mirrors connect_readonly()'s actual default

    # bucket-with-baseline: 5 real fast candidates, but already has a
    # computed baseline row - must not appear in the queue at all.
    for i in range(5):
        _seed_candidate(conn, f"has-baseline-{i}", "1|intel-10th|16", first_seen=1000 + i)
    seed_baseline(conn, bucket_key="1|intel-10th|16")

    # 3 fast candidates, no baseline yet.
    for i in range(3):
        _seed_candidate(conn, f"bucket-b-{i}", "2|intel-11th|16", first_seen=2000 + i)

    # 7 fast candidates, no baseline yet - should rank ABOVE bucket-b.
    for i in range(7):
        _seed_candidate(conn, f"bucket-a-{i}", "3|intel-12th|16", first_seen=3000 + i)

    # Poisoned rows a naive `SELECT bucket_key, COUNT(*) ... GROUP BY
    # bucket_key` would wrongly count, but derive_candidates() correctly
    # excludes: never-swept (first_seen == last_seen) and an incomplete
    # ('?') bucket_key.
    seed_listing(
        conn, "never-swept", bucket_key="4|intel-12th|16",
        first_seen=4000, last_seen=4000, gone_at=4100,
    )
    seed_observation(conn, "never-swept", 4000, price_cents=19000)
    seed_listing(
        conn, "incomplete", bucket_key="4|?|16",
        first_seen=5000, last_seen=5500, gone_at=5600,
    )
    seed_observation(conn, "incomplete", 5400, price_cents=19000)

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=[], limit=10
    )

    bucket_keys = [q["bucket_key"] for q in queue]
    assert "1|intel-10th|16" not in bucket_keys  # already has a baseline
    assert "4|intel-12th|16" not in bucket_keys  # never confirmed by a sweep
    assert "4|?|16" not in bucket_keys  # incomplete bucket_key

    assert [q["bucket_key"] for q in queue] == ["3|intel-12th|16", "2|intel-11th|16"]
    assert queue[0]["fast_candidates"] == 7
    assert queue[1]["fast_candidates"] == 3
    assert queue[0]["min_samples"] == 12

    assert conn.row_factory is None  # restored, not left flipped


def test_baseline_queue_ranks_by_fast_count_not_total_dead_count(tmp_path):
    # V0.11b Part A: the exact live-data shape that motivated this fix -
    # "1|intel-10th|8" has more TOTAL dead listings (21) than
    # "1|amd-ryzen-4000|16" (15), but far fewer FAST ones (6 vs 11).
    # Ranking by total count puts the wrong bucket on top - the second
    # is one fast candidate away from a computed baseline (min_samples
    # 12), and the panel exists to point at exactly that bucket. A
    # fixture where both orderings agree would prove nothing; this one
    # is built so the two orders genuinely disagree.
    conn = make_conn(tmp_path)
    conn.row_factory = None

    MORE_DEAD_FEWER_FAST = "1|intel-10th|8"
    FEWER_DEAD_MORE_FAST = "1|amd-ryzen-4000|16"

    for i in range(6):
        _seed_candidate(
            conn, f"a-fast-{i}", MORE_DEAD_FEWER_FAST, first_seen=1000 + i, lifespan_seconds=600
        )
    for i in range(15):
        _seed_candidate(
            conn, f"a-slow-{i}", MORE_DEAD_FEWER_FAST, first_seen=2000 + i,
            lifespan_seconds=100 * 3600,  # 100h - slow at a 24h threshold
        )
    # 21 total dead in this bucket, only 6 fast.

    for i in range(11):
        _seed_candidate(
            conn, f"b-fast-{i}", FEWER_DEAD_MORE_FAST, first_seen=3000 + i, lifespan_seconds=600
        )
    for i in range(4):
        _seed_candidate(
            conn, f"b-slow-{i}", FEWER_DEAD_MORE_FAST, first_seen=4000 + i,
            lifespan_seconds=100 * 3600,
        )
    # 15 total dead in this bucket, 11 fast.

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=[], limit=10
    )

    # Total-dead ordering would put MORE_DEAD_FEWER_FAST (21) first.
    # Fast-count ordering (the fix) puts FEWER_DEAD_MORE_FAST (11) first.
    assert [q["bucket_key"] for q in queue] == [FEWER_DEAD_MORE_FAST, MORE_DEAD_FEWER_FAST]
    by_bucket = {q["bucket_key"]: q["fast_candidates"] for q in queue}
    assert by_bucket[FEWER_DEAD_MORE_FAST] == 11
    assert by_bucket[MORE_DEAD_FEWER_FAST] == 6


def test_baseline_queue_respects_limit(tmp_path):
    conn = make_conn(tmp_path)
    conn.row_factory = None
    for b in range(15):
        _seed_candidate(conn, f"item-{b}", f"bucket-{b}", first_seen=1000 + b)

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=[], limit=5
    )

    assert len(queue) == 5


def test_baseline_queue_restores_row_factory_when_derive_raises(tmp_path, monkeypatch):
    conn = make_conn(tmp_path)
    conn.row_factory = None

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated derive failure")

    monkeypatch.setattr(panels, "derive_candidates", _boom)

    with pytest.raises(RuntimeError):
        panels.baseline_queue(
            conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=[]
        )

    assert conn.row_factory is None  # restored even on the raising path


# ---------------------------------------------------------------------------
# baseline_queue - progress bar clamp and seed values (V0.12 Parts A/B)
# ---------------------------------------------------------------------------


def test_baseline_queue_progress_pct_is_clamped_but_fast_candidates_is_not(tmp_path):
    # V0.12 Part A1: recompute_baselines.py is manual, so a bucket can sit
    # past min_samples for days with nobody having run it - that's
    # exactly the signal this test protects. 13 fast candidates against
    # min_samples=12 must show 13 (not clamped to 12) with a bar clamped
    # at 100 (not 108.3).
    conn = make_conn(tmp_path)
    conn.row_factory = None
    for i in range(13):
        _seed_candidate(conn, f"item-{i}", "1|intel-10th|16", first_seen=1000 + i)

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=[]
    )

    assert len(queue) == 1
    assert queue[0]["fast_candidates"] == 13  # the true count, never clamped
    assert queue[0]["progress_pct"] == 100.0  # the bar, clamped


def test_baseline_queue_progress_pct_below_threshold_is_not_clamped(tmp_path):
    conn = make_conn(tmp_path)
    conn.row_factory = None
    for i in range(3):
        _seed_candidate(conn, f"item-{i}", "1|intel-10th|16", first_seen=1000 + i)

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=[]
    )

    assert queue[0]["fast_candidates"] == 3
    assert queue[0]["progress_pct"] == pytest.approx(25.0)  # 3/12, no clamping needed


def test_baseline_queue_resolves_the_best_matching_seed_not_the_fallback(tmp_path):
    # V0.12 Part B1: a bucket whose candidates' spec matches a SPECIFIC
    # seed entry must get that seed, not the {} universal fallback -
    # proves resolve_seed_baseline()'s most-matched-keys rule is actually
    # being exercised here, not bypassed.
    conn = make_conn(tmp_path)
    conn.row_factory = None
    spec = {"generation": "5", "cpu_family": "intel-ultra-1"}
    for i in range(3):
        _seed_candidate(conn, f"item-{i}", "5|intel-ultra-1|32", first_seen=1000 + i, spec=spec)

    seeds = make_seeds(
        [
            {"match": {}, "p25": 100, "p50": 150},
            {"match": {"generation": "5", "cpu_family": "intel-ultra-1"}, "p25": 525, "p50": 625},
        ]
    )

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=seeds
    )

    assert queue[0]["seed_p25_display"] == "$525.00"
    assert queue[0]["seed_p50_display"] == "$625.00"


def test_baseline_queue_seed_resolution_uses_best_match_not_first_generation_match(tmp_path):
    # Deliberately overlapping match blocks (per the milestone's own
    # warning: a fixture where a naive check and the real algorithm
    # happen to agree proves nothing). A coarse generation-only entry
    # comes FIRST in file order and would win under any "first seed
    # whose generation matches" shortcut; a second entry's match block
    # requires a DIFFERENT cpu_family than this spec has, so it fails
    # entirely under the real algorithm (all() over its own match dict),
    # not just a weaker one. Only the third entry actually matches every
    # field of its own match block against this spec - the real
    # best-matched-keys-wins algorithm must pick it over the coarser
    # first entry.
    conn = make_conn(tmp_path)
    conn.row_factory = None
    spec = {"generation": "5", "cpu_family": "intel-ultra-1"}
    for i in range(3):
        _seed_candidate(conn, f"item-{i}", "5|intel-ultra-1|32", first_seen=1000 + i, spec=spec)

    seeds = make_seeds(
        [
            {"match": {"generation": "5"}, "p25": 400, "p50": 450},
            {"match": {"generation": "5", "cpu_family": "amd-ryzen-8000"}, "p25": 300, "p50": 350},
            {"match": {"generation": "5", "cpu_family": "intel-ultra-1"}, "p25": 525, "p50": 625},
        ]
    )

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=seeds
    )

    assert queue[0]["seed_p25_display"] == "$525.00"
    assert queue[0]["seed_p50_display"] == "$625.00"


def test_baseline_queue_ram_agnostic_buckets_share_the_same_seed(tmp_path):
    # V0.12 Part B3: seed_baselines has no RAM dimension - two buckets
    # differing only in ram_tier must resolve to the IDENTICAL seed. This
    # pins that behavior deliberately, so a future reader doesn't "fix"
    # what looks like duplicate values by mistake.
    conn = make_conn(tmp_path)
    conn.row_factory = None
    spec = {"generation": "1", "cpu_family": "intel-10th"}
    for i in range(3):
        _seed_candidate(conn, f"a-{i}", "1|intel-10th|8", first_seen=1000 + i, spec=spec)
    for i in range(3):
        _seed_candidate(conn, f"b-{i}", "1|intel-10th|32", first_seen=2000 + i, spec=spec)

    seeds = make_seeds(
        [{"match": {"generation": "1", "cpu_family": "intel-10th"}, "p25": 165, "p50": 195}]
    )

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=seeds
    )

    by_bucket = {q["bucket_key"]: q for q in queue}
    assert by_bucket["1|intel-10th|8"]["seed_p25_display"] == "$165.00"
    assert by_bucket["1|intel-10th|32"]["seed_p25_display"] == "$165.00"
    assert by_bucket["1|intel-10th|8"]["seed_p50_display"] == "$195.00"
    assert by_bucket["1|intel-10th|32"]["seed_p50_display"] == "$195.00"


def test_baseline_queue_picks_the_representative_listing_deterministically(tmp_path):
    # Follow-up to V0.12 Part B: the representative candidate for a
    # bucket's seed lookup must be chosen deterministically (min item_id),
    # not whichever row derive_candidates() happens to return first -
    # _DEAD_OK_LISTINGS (engine/baselines.py) has no ORDER BY, so
    # insertion order is an implementation artifact, not a guarantee.
    #
    # Seeds two candidates in the SAME bucket with deliberately DIFFERENT
    # spec_json - not a realistic shape today (see the invariant comment
    # at baseline_queue()'s own call site: this can only happen once a
    # seed matches on a field outside the bucket_key), but the selection
    # mechanism itself must be pinned independent of whether that
    # invariant currently holds. Inserted in an order where the
    # lexicographically LATER item_id is the one a bare "first row
    # returned" bug would pick.
    conn = make_conn(tmp_path)
    conn.row_factory = None

    _seed_candidate(
        conn, "zzz-item", "1|intel-10th|16", first_seen=1000,
        spec={"generation": "9", "cpu_family": "unknown"},  # resolves to the {} fallback
    )
    _seed_candidate(
        conn, "aaa-item", "1|intel-10th|16", first_seen=1001,
        spec={"generation": "1", "cpu_family": "intel-10th"},  # resolves to the specific match
    )

    seeds = make_seeds(
        [
            {"match": {}, "p25": 100, "p50": 150},
            {"match": {"generation": "1", "cpu_family": "intel-10th"}, "p25": 165, "p50": 195},
        ]
    )

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=seeds
    )

    # "aaa-item" (min item_id) must drive resolution, regardless of the
    # fact that "zzz-item" was inserted - and therefore returned by
    # derive_candidates() - first.
    assert queue[0]["seed_p25_display"] == "$165.00"
    assert queue[0]["seed_p50_display"] == "$195.00"


def test_baseline_queue_renders_explicit_unresolved_state_with_no_fallback(tmp_path):
    # V0.12 Part B4: a profile with no `match: {}` fallback entry means
    # resolve_seed_baseline() can return None for a bucket whose spec
    # matches nothing more specific. Must render an explicit "unresolved"
    # state, never a blank string or a fabricated $0.00 - same "absent is
    # not zero" discipline as indicators.py's unknown state.
    conn = make_conn(tmp_path)
    conn.row_factory = None
    for i in range(3):
        _seed_candidate(
            conn, f"item-{i}", "9|unknown-cpu|16", first_seen=1000 + i,
            spec={"generation": "9", "cpu_family": "unknown-cpu"},
        )

    seeds = make_seeds([{"match": {"generation": "1"}, "p25": 165, "p50": 195}])  # no {} fallback

    queue = panels.baseline_queue(
        conn, PROFILE, min_samples=12, fast_lifespan_hours=24, compiled_seeds=seeds
    )

    assert queue[0]["seed_p25_display"] == "unresolved"
    assert queue[0]["seed_p50_display"] == "unresolved"


# ---------------------------------------------------------------------------
# best_ratio_window() (V0.12 Part C4)
# ---------------------------------------------------------------------------


def _seed_alert_ratio(conn, item_id, *, sent_at, ratio_to_p25, profile_id=PROFILE):
    seed_listing(conn, item_id, profile_id=profile_id)
    seed_alert(conn, item_id, profile_id=profile_id, sent_at=sent_at, ratio_to_p25=ratio_to_p25)


def test_best_ratio_window_returns_none_for_an_empty_window(tmp_path):
    conn = make_conn(tmp_path)

    assert panels.best_ratio_window(conn, PROFILE, days=14, now=_NOON) is None


def test_best_ratio_window_returns_the_minimum_ratio_in_the_window(tmp_path):
    conn = make_conn(tmp_path)
    _seed_alert_ratio(conn, "item-1", sent_at=_TODAY_START + 100, ratio_to_p25=0.90)
    _seed_alert_ratio(conn, "item-2", sent_at=_TODAY_START + 200, ratio_to_p25=0.72)  # best
    _seed_alert_ratio(conn, "item-3", sent_at=_TODAY_START + 300, ratio_to_p25=0.95)

    result = panels.best_ratio_window(conn, PROFILE, days=14, now=_NOON)

    assert result == pytest.approx(0.72)


def test_best_ratio_window_excludes_alerts_outside_the_window(tmp_path):
    conn = make_conn(tmp_path)
    # 20 days before `now` - outside a 14-day window.
    _seed_alert_ratio(conn, "old-item", sent_at=_TODAY_START - 20 * 86400, ratio_to_p25=0.10)
    _seed_alert_ratio(conn, "in-window", sent_at=_TODAY_START + 100, ratio_to_p25=0.80)

    result = panels.best_ratio_window(conn, PROFILE, days=14, now=_NOON)

    assert result == pytest.approx(0.80)  # not the 0.10 outside the window
