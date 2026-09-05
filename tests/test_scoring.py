"""Tests for dealwatch.engine.scoring (design.md §5.6, V0.8b).

Real SQLite under tmp_path for the computed-baseline layer (store_baselines
is the real write path); seed-baseline tests need no DB writes at all. No
network.
"""

import pytest

from dealwatch.engine.baselines import Baseline
from dealwatch.engine.scoring import (
    CompiledSeedBaseline,
    compile_seed_baselines,
    resolve_seed_baseline,
    score_listing,
)
from dealwatch.normalize.engine import ProfileCompileError
from dealwatch.normalize.schema import PollConfig, Profile, SearchConfig
from dealwatch.storage.sqlite import connect, store_baselines

PROFILE_ID = "thinkpad-t14"
BUCKET = "1|intel-10th|16|256"


def make_profile(*, seed_baselines=None, sanity_floor_pct=35):
    return Profile(
        id=PROFILE_ID,
        name="Test Profile",
        search=SearchConfig(queries=["q"], filters={}, poll=PollConfig()),
        scoring={"sanity_floor_pct": sanity_floor_pct, "target_percentile": 25},
        seed_baselines=seed_baselines or [],
    )


def make_conn(tmp_path):
    return connect(tmp_path / "dealwatch.db")


def seed_computed_baseline(conn, bucket_key, *, n=12, p25_cents, p50_cents):
    store_baselines(
        conn,
        PROFILE_ID,
        [
            Baseline(
                bucket_key=bucket_key, n=n, n_price_only=0,
                p10_cents=p25_cents - 100, p25_cents=p25_cents,
                p50_cents=p50_cents, fast_hours=24,
            )
        ],
        computed_at=1000,
    )


# ---------------------------------------------------------------------------
# Seed matching
# ---------------------------------------------------------------------------


def test_most_specific_seed_wins():
    seeds = compile_seed_baselines(
        make_profile(
            seed_baselines=[
                {"match": {"a": 1}, "p25": 10, "p50": 20},
                {"match": {"a": 1, "b": 2}, "p25": 30, "p50": 40},
            ]
        )
    )
    winner = resolve_seed_baseline(seeds, {"a": 1, "b": 2})
    assert winner.p25_cents == 3000  # the two-key match, not the one-key match


def test_empty_match_is_the_fallback():
    seeds = compile_seed_baselines(
        make_profile(
            seed_baselines=[
                {"match": {"a": 1}, "p25": 10, "p50": 20},
                {"match": {}, "p25": 5, "p50": 6},
            ]
        )
    )
    winner = resolve_seed_baseline(seeds, {"a": 99})  # matches nothing specific
    assert winner.p25_cents == 500


def test_tie_broken_by_file_order():
    seeds = compile_seed_baselines(
        make_profile(
            seed_baselines=[
                {"match": {"a": 1}, "p25": 1, "p50": 2},   # first, score 1
                {"match": {"b": 2}, "p25": 3, "p50": 4},   # also score 1
            ]
        )
    )
    winner = resolve_seed_baseline(seeds, {"a": 1, "b": 2})
    assert winner.p25_cents == 100  # the first entry, not the second


def test_duplicate_match_blocks_raise_at_compile():
    profile = make_profile(
        seed_baselines=[
            {"match": {"a": 1}, "p25": 10, "p50": 20},
            {"match": {"a": 1}, "p25": 30, "p50": 40},
        ]
    )
    with pytest.raises(ProfileCompileError):
        compile_seed_baselines(profile)


def test_dollars_converted_to_cents_at_compile():
    seeds = compile_seed_baselines(
        make_profile(seed_baselines=[{"match": {}, "p25": 220, "p50": 280.5}])
    )
    assert seeds[0].p25_cents == 22000
    assert seeds[0].p50_cents == 28050


def test_no_match_returns_none():
    seeds = compile_seed_baselines(
        make_profile(seed_baselines=[{"match": {"a": 1}, "p25": 10, "p50": 20}])
    )
    assert resolve_seed_baseline(seeds, {"a": 2}) is None


# ---------------------------------------------------------------------------
# The fallback ladder
# ---------------------------------------------------------------------------


def test_computed_baseline_preferred_when_present(tmp_path):
    conn = make_conn(tmp_path)
    seed_computed_baseline(conn, BUCKET, n=12, p25_cents=20000, p50_cents=25000)
    profile = make_profile(seed_baselines=[{"match": {}, "p25": 999, "p50": 999}])
    seeds = compile_seed_baselines(profile)

    result = score_listing(
        conn, profile, seeds,
        item_id="item-1", bucket_key=BUCKET, spec={},
        price_cents=15000, price_is_price_only=False, item_web_url=None,
    )

    assert result.baseline_layer == "computed"
    assert result.baseline_match == BUCKET
    assert result.baseline_n == 12
    assert result.baseline_p25_cents == 20000
    assert result.baseline_p50_cents == 25000


def test_seed_used_when_computed_absent(tmp_path):
    conn = make_conn(tmp_path)  # no baselines rows at all
    profile = make_profile(
        seed_baselines=[{"match": {"generation": "1"}, "p25": 220, "p50": 280}]
    )
    seeds = compile_seed_baselines(profile)

    result = score_listing(
        conn, profile, seeds,
        item_id="item-1", bucket_key=BUCKET, spec={"generation": "1"},
        price_cents=20000, price_is_price_only=False, item_web_url=None,
    )

    assert result.baseline_layer == "seed"
    assert result.baseline_n is None
    assert result.baseline_p25_cents == 22000
    assert result.baseline_p50_cents == 28000
    assert result.baseline_match == str({"generation": "1"})


def test_question_mark_bucket_always_falls_to_seed(tmp_path):
    conn = make_conn(tmp_path)
    qmark_bucket = "1|?|16|256"
    # Contrived: a row under a '?' bucket_key should never exist in real
    # data (V0.8a excludes those from baseline computation), but insert one
    # directly to prove the ladder itself refuses to use it, not just that
    # one happens to be absent.
    conn.execute(
        "INSERT INTO baselines (profile_id, bucket_key, n, n_price_only, "
        "p10_cents, p25_cents, p50_cents, fast_hours, computed_at) "
        "VALUES (?, ?, 12, 0, 100, 200, 300, 24, 1000)",
        (PROFILE_ID, qmark_bucket),
    )
    profile = make_profile(seed_baselines=[{"match": {}, "p25": 220, "p50": 280}])
    seeds = compile_seed_baselines(profile)

    result = score_listing(
        conn, profile, seeds,
        item_id="item-1", bucket_key=qmark_bucket, spec={},
        price_cents=20000, price_is_price_only=False, item_web_url=None,
    )

    assert result.baseline_layer == "seed"
    assert result.baseline_p25_cents == 22000


# ---------------------------------------------------------------------------
# Sanity floor - exact boundary, both layers
# ---------------------------------------------------------------------------


def test_sanity_floor_fires_just_below_threshold_on_computed_layer(tmp_path):
    conn = make_conn(tmp_path)
    seed_computed_baseline(conn, BUCKET, p25_cents=8000, p50_cents=10000)
    profile = make_profile(sanity_floor_pct=35)
    seeds = compile_seed_baselines(profile)

    result = score_listing(
        conn, profile, seeds,
        item_id="item-1", bucket_key=BUCKET, spec={},
        price_cents=3499, price_is_price_only=False, item_web_url=None,  # 34.99% of p50
    )

    assert result.sanity_flagged is True


def test_sanity_floor_does_not_fire_exactly_at_threshold_on_computed_layer(tmp_path):
    conn = make_conn(tmp_path)
    seed_computed_baseline(conn, BUCKET, p25_cents=8000, p50_cents=10000)
    profile = make_profile(sanity_floor_pct=35)
    seeds = compile_seed_baselines(profile)

    result = score_listing(
        conn, profile, seeds,
        item_id="item-1", bucket_key=BUCKET, spec={},
        price_cents=3500, price_is_price_only=False, item_web_url=None,  # exactly 35% of p50
    )

    assert result.sanity_flagged is False


def test_sanity_floor_fires_just_below_threshold_on_seed_layer(tmp_path):
    conn = make_conn(tmp_path)
    profile = make_profile(
        sanity_floor_pct=35, seed_baselines=[{"match": {}, "p25": 80, "p50": 100}]
    )
    seeds = compile_seed_baselines(profile)

    result = score_listing(
        conn, profile, seeds,
        item_id="item-1", bucket_key=None, spec={},
        price_cents=3499, price_is_price_only=False, item_web_url=None,
    )

    assert result.baseline_layer == "seed"
    assert result.sanity_flagged is True


def test_sanity_floor_does_not_fire_exactly_at_threshold_on_seed_layer(tmp_path):
    conn = make_conn(tmp_path)
    profile = make_profile(
        sanity_floor_pct=35, seed_baselines=[{"match": {}, "p25": 80, "p50": 100}]
    )
    seeds = compile_seed_baselines(profile)

    result = score_listing(
        conn, profile, seeds,
        item_id="item-1", bucket_key=None, spec={},
        price_cents=3500, price_is_price_only=False, item_web_url=None,
    )

    assert result.sanity_flagged is False


# ---------------------------------------------------------------------------
# Pass-through fields
# ---------------------------------------------------------------------------


def test_price_is_price_only_is_passed_through(tmp_path):
    conn = make_conn(tmp_path)
    profile = make_profile(seed_baselines=[{"match": {}, "p25": 100, "p50": 150}])
    seeds = compile_seed_baselines(profile)

    result = score_listing(
        conn, profile, seeds,
        item_id="item-1", bucket_key=None, spec={},
        price_cents=12000, price_is_price_only=True, item_web_url="https://ebay.com/itm/1",
    )

    assert result.price_is_price_only is True
    assert result.price_cents == 12000
    assert result.item_web_url == "https://ebay.com/itm/1"
    assert result.ratio_to_p25 == pytest.approx(12000 / 10000)
    assert result.ratio_to_p50 == pytest.approx(12000 / 15000)
