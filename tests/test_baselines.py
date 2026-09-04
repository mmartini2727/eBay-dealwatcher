"""Tests for dealwatch.engine.baselines (V0.8a, design.md §2.1).

Real SQLite under tmp_path via the real write path (record_sighting,
store_spec) rather than hand-crafted rows, so these tests exercise the
same schema the collector actually produces. No network.
"""

import json

import pytest

from dealwatch.engine.baselines import (
    Baseline,
    compute_baselines,
    derive_candidate_pool_stats,
    derive_candidates,
    nearest_rank_percentile,
)
from dealwatch.normalize.engine import SpecResult
from dealwatch.storage.sqlite import connect, record_sighting, store_baselines, store_spec

PROFILE_ID = "thinkpad-t14"
BUCKET = "1|intel-10th|16|256"


def make_conn(tmp_path):
    return connect(tmp_path / "dealwatch.db")


def sight(conn, item_id, seen_at, *, price_cents=None, shipping_cents=None):
    total_cents = None
    if price_cents is not None and shipping_cents is not None:
        total_cents = price_cents + shipping_cents
    raw = {"itemId": item_id, "title": "t"}
    if price_cents is not None:
        raw["price"] = {"value": f"{price_cents / 100:.2f}"}
    record_sighting(
        conn,
        item_id,
        dict(profile_id=PROFILE_ID, title="t"),
        dict(
            price_cents=price_cents,
            shipping_cents=shipping_cents,
            total_cents=total_cents,
            raw_json=json.dumps(raw),
        ),
        seen_at,
    )


def mark_gone(conn, item_id, gone_at):
    conn.execute("UPDATE listings SET gone_at = ? WHERE item_id = ?", (gone_at, item_id))


def set_spec(conn, item_id, bucket_key, spec_status="ok"):
    store_spec(
        conn,
        item_id,
        SpecResult(spec={}, spec_status=spec_status, reject_rule_id=None, bucket_key=bucket_key),
    )


# ---------------------------------------------------------------------------
# derive_candidates()
# ---------------------------------------------------------------------------


def test_price_cut_produces_one_candidate_from_the_last_observation_only(tmp_path):
    # design.md §2.1's exact scenario: $900 for 2h, cut to $700, dies 30h
    # after the cut. Must be ONE candidate at $700/30h - not $900, and not
    # 32h attributed to $700 (the cut ended the $900 price point, not a sale).
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "item-1", t0, price_cents=90000)
    sight(conn, "item-1", t0 + 2 * 3600, price_cents=70000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", t0 + 2 * 3600 + 30 * 3600)

    candidates = derive_candidates(conn)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 70000
    assert candidates[0].lifespan_seconds == 30 * 3600


def test_single_observation_lifespan_is_gone_at_minus_that_observation(tmp_path):
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "item-1", t0, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", t0 + 5 * 3600)

    candidates = derive_candidates(conn)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 50000
    assert candidates[0].lifespan_seconds == 5 * 3600


def test_live_listing_is_excluded(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    # never marked gone

    assert derive_candidates(conn) == []


@pytest.mark.parametrize("spec_status", ["partial", "pending"])
def test_non_ok_spec_status_is_excluded(tmp_path, spec_status):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000)
    # bucket_key set deliberately, to isolate that spec_status itself gates
    # this, not an incidentally-null bucket_key.
    set_spec(conn, "item-1", BUCKET, spec_status=spec_status)
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    assert derive_candidates(conn) == []


def test_bucket_key_with_question_mark_is_excluded(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000)
    set_spec(conn, "item-1", "1|?|16|256")
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    assert derive_candidates(conn) == []


def test_null_bucket_key_is_excluded(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000)
    set_spec(conn, "item-1", None, spec_status="ok")
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    assert derive_candidates(conn) == []


def test_null_shipping_uses_price_cents_and_counts_as_price_only(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000, shipping_cents=None)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    candidates = derive_candidates(conn)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 50000
    assert candidates[0].price_is_price_only is True


def test_known_shipping_uses_total_cents_and_is_not_price_only(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000, shipping_cents=1000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    candidates = derive_candidates(conn)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 51000
    assert candidates[0].price_is_price_only is False


def test_both_prices_null_is_dropped(tmp_path):
    # An auction row with no price field at all.
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=None, shipping_cents=None)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    assert derive_candidates(conn) == []


def test_negative_lifespan_is_dropped_and_logged(tmp_path, caplog):
    import logging

    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "item-1", t0, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", t0 - 3600)  # gone_at BEFORE the observation

    with caplog.at_level(logging.WARNING):
        candidates = derive_candidates(conn)

    assert candidates == []
    assert any(
        record.levelname == "WARNING" and "item-1" in record.message
        for record in caplog.records
    )


def test_candidate_pool_stats_breakdown_matches_final_candidate_count(tmp_path):
    conn = make_conn(tmp_path)
    t0 = 1_000_000

    sight(conn, "ok1", t0, price_cents=50000)
    set_spec(conn, "ok1", BUCKET)
    mark_gone(conn, "ok1", t0 + 3600)

    sight(conn, "questionmark", t0, price_cents=50000)
    set_spec(conn, "questionmark", "1|?|16|256")
    mark_gone(conn, "questionmark", t0 + 3600)

    sight(conn, "nobucket", t0, price_cents=50000)
    set_spec(conn, "nobucket", None)
    mark_gone(conn, "nobucket", t0 + 3600)

    sight(conn, "noprice", t0, price_cents=None)
    set_spec(conn, "noprice", BUCKET)
    mark_gone(conn, "noprice", t0 + 3600)

    stats = derive_candidate_pool_stats(conn)
    candidates = derive_candidates(conn)

    assert stats.total_dead_ok == 4
    assert stats.has_bucket_key == 3  # excludes nobucket
    assert stats.bucket_key_has_no_question_mark == 2  # excludes questionmark too
    assert stats.has_usable_price == 1  # excludes noprice too
    assert len(candidates) == 1


# ---------------------------------------------------------------------------
# nearest_rank_percentile()
# ---------------------------------------------------------------------------


def test_nearest_rank_percentile_matches_hand_computed_values():
    values = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]  # n=10
    assert nearest_rank_percentile(values, 10) == 10
    assert nearest_rank_percentile(values, 25) == 30
    assert nearest_rank_percentile(values, 50) == 50

    odd = [1, 2, 3, 4, 5, 6, 7]  # n=7, exercises ceiling rounding
    assert nearest_rank_percentile(odd, 10) == 1
    assert nearest_rank_percentile(odd, 25) == 2
    assert nearest_rank_percentile(odd, 50) == 4


def test_nearest_rank_percentile_single_value_never_indexes_out_of_range():
    assert nearest_rank_percentile([42], 10) == 42
    assert nearest_rank_percentile([42], 99) == 42


# ---------------------------------------------------------------------------
# compute_baselines() - min_samples applies to the FAST population
# ---------------------------------------------------------------------------


def _seed_fast_candidates(conn, tmp_path, count, *, start_price=10000):
    t0 = 1_000_000
    for i in range(count):
        item_id = f"item-{i}"
        sight(conn, item_id, t0, price_cents=start_price + i * 100)
        set_spec(conn, item_id, BUCKET)
        mark_gone(conn, item_id, t0 + 3600)  # 1h - well inside any threshold


def test_min_samples_boundary_eleven_no_row_twelve_a_row(tmp_path):
    conn = make_conn(tmp_path)
    _seed_fast_candidates(conn, tmp_path, 11)

    candidates = derive_candidates(conn)
    assert len(candidates) == 11
    assert compute_baselines(candidates, fast_lifespan_hours=24, min_samples=12) == []

    sight(conn, "item-11", 1_000_000, price_cents=10000 + 11 * 100)
    set_spec(conn, "item-11", BUCKET)
    mark_gone(conn, "item-11", 1_000_000 + 3600)

    candidates = derive_candidates(conn)
    assert len(candidates) == 12
    baselines = compute_baselines(candidates, fast_lifespan_hours=24, min_samples=12)
    assert len(baselines) == 1
    assert baselines[0].n == 12
    assert baselines[0].bucket_key == BUCKET


def test_slow_candidates_do_not_count_toward_min_samples(tmp_path):
    # min_samples applies to the FAST population specifically - a bucket
    # with plenty of dead listings but few fast ones must not qualify.
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    for i in range(20):
        item_id = f"slow-{i}"
        sight(conn, item_id, t0, price_cents=50000)
        set_spec(conn, item_id, BUCKET)
        mark_gone(conn, item_id, t0 + 100 * 3600)  # 100h - slow at a 24h threshold

    candidates = derive_candidates(conn)
    assert len(candidates) == 20
    assert compute_baselines(candidates, fast_lifespan_hours=24, min_samples=12) == []


def test_mixed_bucket_with_enough_dead_but_too_few_fast_does_not_qualify(tmp_path):
    # Isolates the min_samples check from the pure-slow case above: this
    # bucket has 25 dead candidates total (>= min_samples on its own), but
    # only 5 are fast. A bug that checked the dead count instead of the
    # fast count would wrongly produce a baseline here from just 5 prices.
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    for i in range(5):
        item_id = f"fast-{i}"
        sight(conn, item_id, t0, price_cents=20000 + i * 10)
        set_spec(conn, item_id, BUCKET)
        mark_gone(conn, item_id, t0 + 3600)  # 1h - fast
    for i in range(20):
        item_id = f"slow-{i}"
        sight(conn, item_id, t0, price_cents=40000 + i * 10)
        set_spec(conn, item_id, BUCKET)
        mark_gone(conn, item_id, t0 + 100 * 3600)  # 100h - slow

    candidates = derive_candidates(conn)
    assert len(candidates) == 25
    assert compute_baselines(candidates, fast_lifespan_hours=24, min_samples=12) == []


# ---------------------------------------------------------------------------
# store_baselines() - DELETE + INSERT, idempotent
# ---------------------------------------------------------------------------


def test_store_baselines_is_idempotent(tmp_path):
    conn = make_conn(tmp_path)
    baselines = [
        Baseline(
            bucket_key=BUCKET, n=12, n_price_only=12,
            p10_cents=10100, p25_cents=10300, p50_cents=10600, fast_hours=24,
        )
    ]

    store_baselines(conn, PROFILE_ID, baselines, computed_at=1000)
    first = [dict(r) for r in conn.execute("SELECT * FROM baselines").fetchall()]

    store_baselines(conn, PROFILE_ID, baselines, computed_at=1000)
    second = [dict(r) for r in conn.execute("SELECT * FROM baselines").fetchall()]

    assert first == second
    assert len(first) == 1


def test_store_baselines_replaces_not_accumulates(tmp_path):
    conn = make_conn(tmp_path)
    first_run = [
        Baseline(bucket_key=BUCKET, n=12, n_price_only=0, p10_cents=1, p25_cents=2, p50_cents=3, fast_hours=24)
    ]
    store_baselines(conn, PROFILE_ID, first_run, computed_at=1000)

    second_run = [
        Baseline(bucket_key="2|intel-11th|16|512", n=15, n_price_only=1, p10_cents=4, p25_cents=5, p50_cents=6, fast_hours=24)
    ]
    store_baselines(conn, PROFILE_ID, second_run, computed_at=2000)

    rows = conn.execute("SELECT bucket_key FROM baselines WHERE profile_id = ?", (PROFILE_ID,)).fetchall()
    # The first run's bucket must be gone, not still present alongside the second.
    assert [r["bucket_key"] for r in rows] == ["2|intel-11th|16|512"]
