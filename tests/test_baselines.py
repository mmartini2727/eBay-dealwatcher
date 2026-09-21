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
    derive_candidates_with_stats,
    group_fast_candidates_by_bucket,
    nearest_rank_percentile,
)
from dealwatch.normalize.engine import SpecResult
from dealwatch.normalize.listing import parse_variation_id
from dealwatch.storage.sqlite import connect, record_sighting, store_baselines, store_spec

PROFILE_ID = "thinkpad-t14"
BUCKET = "1|intel-10th|16|256"


def make_conn(tmp_path):
    return connect(tmp_path / "dealwatch.db")


def sight(conn, item_id, seen_at, *, price_cents=None, shipping_cents=None, profile_id=PROFILE_ID):
    total_cents = None
    if price_cents is not None and shipping_cents is not None:
        total_cents = price_cents + shipping_cents
    raw = {"itemId": item_id, "title": "t"}
    if price_cents is not None:
        raw["price"] = {"value": f"{price_cents / 100:.2f}"}
    record_sighting(
        conn,
        item_id,
        # variation_id derived from item_id via the real parse_variation_id()
        # (V0.8d), not injected as a separate fixture parameter - production
        # never gets to choose it independently of item_id, and a test that
        # could would no longer catch parse_variation_id() regressing to
        # "always None" (see the exclusion test below). profile_id defaults
        # to the module constant so every existing call site is unaffected;
        # the two-profile test below is the one caller that passes a real
        # second value. record_sighting()'s existence check is on item_id
        # ALONE (design.md §16 P6's first, separately-scoped prerequisite -
        # not touched by this task), so two profiles must never share an
        # item_id in a fixture or the second sight() would silently UPDATE
        # the first profile's row instead of inserting a second one.
        dict(profile_id=profile_id, title="t", variation_id=parse_variation_id(item_id)),
        dict(
            price_cents=price_cents,
            shipping_cents=shipping_cents,
            total_cents=total_cents,
            raw_json=json.dumps(raw),
        ),
        seen_at,
    )


def mark_gone(conn, item_id, gone_at):
    """gone_at = last_seen is an invariant in production (record_sweep sets
    both from the same event, storage/sqlite.py) - this fixture enforces
    it too, rather than leaving last_seen wherever sight() put it (which
    would make every fixture here look "never confirmed by a sweep" to
    V0.8b's new exclusion, whether the test intends that or not). Pass the
    SAME timestamp sight() used to simulate a listing that was never
    confirmed by any sweep; pass a later one (the normal case in this
    file) to simulate at least one sweep confirmation before it vanished."""
    conn.execute(
        "UPDATE listings SET gone_at = ?, last_seen = ? WHERE item_id = ?",
        (gone_at, gone_at, item_id),
    )


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

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 70000
    assert candidates[0].lifespan_seconds == 30 * 3600


def test_single_observation_lifespan_is_gone_at_minus_that_observation(tmp_path):
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "item-1", t0, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", t0 + 5 * 3600)

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 50000
    assert candidates[0].lifespan_seconds == 5 * 3600


def test_never_swept_listing_is_excluded(tmp_path):
    # V0.8b: first_seen == last_seen means no sweep ever confirmed this
    # listing present - gone_at was set straight from the insert
    # timestamp, so the derived lifespan would be 0.0, the fastest
    # possible value, in exactly the band the baseline weighs most
    # heavily. Passing the SAME t0 to mark_gone as sight() used is what
    # produces that never-confirmed shape (see mark_gone's docstring).
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "item-1", t0, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", t0)

    assert derive_candidates(conn, profile_id=PROFILE_ID) == []


def test_swept_listing_with_otherwise_identical_shape_is_included(tmp_path):
    # The direct contrast to the test above: identical in every way except
    # last_seen actually advanced past first_seen (at least one sweep
    # confirmed it present before it vanished) - this one must NOT be
    # excluded by the new gate.
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "item-1", t0, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", t0 + 3600)

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 50000


def test_live_listing_is_excluded(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    # never marked gone

    assert derive_candidates(conn, profile_id=PROFILE_ID) == []


@pytest.mark.parametrize("spec_status", ["partial", "pending"])
def test_non_ok_spec_status_is_excluded(tmp_path, spec_status):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000)
    # bucket_key set deliberately, to isolate that spec_status itself gates
    # this, not an incidentally-null bucket_key.
    set_spec(conn, "item-1", BUCKET, spec_status=spec_status)
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    assert derive_candidates(conn, profile_id=PROFILE_ID) == []


def test_bucket_key_with_question_mark_is_excluded(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000)
    set_spec(conn, "item-1", "1|?|16|256")
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    assert derive_candidates(conn, profile_id=PROFILE_ID) == []


def test_null_bucket_key_is_excluded(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000)
    set_spec(conn, "item-1", None, spec_status="ok")
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    assert derive_candidates(conn, profile_id=PROFILE_ID) == []


def test_variation_listing_is_excluded(tmp_path):
    # V0.8d: a row for one variation of a multi-variation listing
    # (item_id shaped v1|<listing>|<non-zero variation>) flaps in and out
    # of search results based on which variation eBay happens to surface,
    # independent of the listing actually dying - not survival-signal
    # material. Uses a real eBay-shaped item_id (not an injected flag) so
    # this test exercises parse_variation_id() itself via sight() - a
    # regression there (e.g. parse_variation_id always returning None)
    # would make this test go red too, not just the dedicated parse tests
    # in test_listing.py.
    conn = make_conn(tmp_path)
    t0 = 1_000_000

    sight(conn, "v1|999|456", t0, price_cents=50000)  # a real variation
    set_spec(conn, "v1|999|456", BUCKET)
    mark_gone(conn, "v1|999|456", t0 + 3600)

    sight(conn, "plain-1", t0, price_cents=50000)  # not eBay-variation-shaped -> None
    set_spec(conn, "plain-1", BUCKET)
    mark_gone(conn, "plain-1", t0 + 3600)

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert [c.item_id for c in candidates] == ["plain-1"]


def test_null_shipping_uses_price_cents_and_counts_as_price_only(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000, shipping_cents=None)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 50000
    assert candidates[0].price_is_price_only is True


def test_known_shipping_uses_total_cents_and_is_not_price_only(tmp_path):
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=50000, shipping_cents=1000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert len(candidates) == 1
    assert candidates[0].price_cents == 51000
    assert candidates[0].price_is_price_only is False


def test_both_prices_null_is_dropped(tmp_path):
    # An auction row with no price field at all.
    conn = make_conn(tmp_path)
    sight(conn, "item-1", 1_000_000, price_cents=None, shipping_cents=None)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", 1_000_000 + 3600)

    assert derive_candidates(conn, profile_id=PROFILE_ID) == []


def test_negative_lifespan_is_dropped_and_logged_entirely_at_debug(tmp_path, caplog):
    # V0.13 (design.md's dated entry): the aggregate line itself is now
    # demoted from INFO to DEBUG, alongside the per-item line (V0.11b).
    # Not a volume fix - the count now lives on the dashboard
    # (reporting/panels.py's baseline_queue(), via
    # derive_candidates_with_stats() below) instead of the log, so there
    # is no reader left at INFO for this line at all.
    import logging

    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "item-1", t0, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", t0 - 3600)  # gone_at BEFORE the observation

    with caplog.at_level(logging.DEBUG):
        candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert candidates == []
    debug_records = [
        r for r in caplog.records if r.levelname == "DEBUG" and "item-1" in r.message
    ]
    assert len(debug_records) == 1
    aggregate_records = [
        r for r in caplog.records
        if "dropped 1 candidates: negative lifespan" in r.message
    ]
    assert len(aggregate_records) == 1
    assert aggregate_records[0].levelname == "DEBUG"
    assert not any(r.levelname == "INFO" for r in caplog.records)


def test_negative_lifespan_no_longer_emits_at_warning_level(tmp_path, caplog):
    # Sabotage-adjacent regression: at the level a container actually
    # runs at by default (WARNING, no basicConfig), the per-item message
    # must be silent - only the DEBUG line and the INFO aggregate exist
    # now. Failing this means the demotion didn't happen.
    import logging

    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "item-1", t0, price_cents=50000)
    set_spec(conn, "item-1", BUCKET)
    mark_gone(conn, "item-1", t0 - 3600)

    with caplog.at_level(logging.WARNING):
        derive_candidates(conn, profile_id=PROFILE_ID)

    assert not any(r.levelname == "WARNING" for r in caplog.records)


def test_negative_lifespan_aggregate_counts_all_dropped_items_in_one_line(tmp_path, caplog):
    import logging

    conn = make_conn(tmp_path)
    t0 = 1_000_000
    for i in range(3):
        item_id = f"item-{i}"
        sight(conn, item_id, t0, price_cents=50000)
        set_spec(conn, item_id, BUCKET)
        mark_gone(conn, item_id, t0 - 3600)

    with caplog.at_level(logging.DEBUG):
        candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert candidates == []
    aggregate_records = [
        r for r in caplog.records if "dropped 3 candidates: negative lifespan" in r.message
    ]
    assert len(aggregate_records) == 1  # ONE aggregate line, not one per item
    assert aggregate_records[0].levelname == "DEBUG"


def test_candidate_pool_stats_breakdown_matches_final_candidate_count(tmp_path):
    conn = make_conn(tmp_path)
    t0 = 1_000_000

    sight(conn, "neverswept", t0, price_cents=50000)
    set_spec(conn, "neverswept", BUCKET)
    mark_gone(conn, "neverswept", t0)  # same t0 - never confirmed by a sweep

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

    stats = derive_candidate_pool_stats(conn, profile_id=PROFILE_ID)
    candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert stats.total_dead_ok == 5
    assert stats.sweep_confirmed == 4  # excludes neverswept
    assert stats.has_bucket_key == 3  # excludes nobucket
    assert stats.bucket_key_has_no_question_mark == 2  # excludes questionmark too
    assert stats.has_usable_price == 1  # excludes noprice too
    assert stats.negative_lifespan_dropped == 0  # no negative-lifespan row in this fixture
    assert stats.has_usable_price - stats.negative_lifespan_dropped == len(candidates)


def test_negative_lifespan_dropped_count_is_excluded_from_the_final_candidates(tmp_path):
    # has_usable_price alone no longer equals len(candidates) once a
    # negative-lifespan row exists - negative_lifespan_dropped is the gap
    # between them (CandidatePoolStats's own docstring).
    conn = make_conn(tmp_path)
    t0 = 1_000_000

    sight(conn, "ok1", t0, price_cents=50000)
    set_spec(conn, "ok1", BUCKET)
    mark_gone(conn, "ok1", t0 + 3600)

    sight(conn, "negative", t0, price_cents=50000)
    set_spec(conn, "negative", BUCKET)
    mark_gone(conn, "negative", t0 - 3600)  # gone_at BEFORE the observation

    stats = derive_candidate_pool_stats(conn, profile_id=PROFILE_ID)
    candidates = derive_candidates(conn, profile_id=PROFILE_ID)

    assert stats.has_usable_price == 2
    assert stats.negative_lifespan_dropped == 1
    assert len(candidates) == 1
    assert stats.has_usable_price - stats.negative_lifespan_dropped == len(candidates)


def test_derive_candidates_with_stats_matches_the_two_separate_calls(tmp_path):
    # V0.13: derive_candidates_with_stats() must be exactly what calling
    # derive_candidates() and derive_candidate_pool_stats() separately
    # already returns - it is _derive() itself, not a second
    # implementation of the exclusion pipeline.
    conn = make_conn(tmp_path)
    t0 = 1_000_000

    sight(conn, "ok1", t0, price_cents=50000)
    set_spec(conn, "ok1", BUCKET)
    mark_gone(conn, "ok1", t0 + 3600)

    sight(conn, "negative", t0, price_cents=50000)
    set_spec(conn, "negative", BUCKET)
    mark_gone(conn, "negative", t0 - 3600)

    candidates_separate = derive_candidates(conn, profile_id=PROFILE_ID)
    stats_separate = derive_candidate_pool_stats(conn, profile_id=PROFILE_ID)

    candidates_combined, stats_combined = derive_candidates_with_stats(conn, profile_id=PROFILE_ID)

    assert candidates_combined == candidates_separate
    assert stats_combined == stats_separate


# ---------------------------------------------------------------------------
# profile_id scoping (design.md §16 P6, docs/learnings.md L2)
# ---------------------------------------------------------------------------

OTHER_PROFILE_ID = "other-profile"


def test_candidates_and_percentiles_do_not_mix_across_profiles_even_with_a_colliding_bucket_key(
    tmp_path,
):
    """The required L2 test: two profiles, dead candidates landing in the
    SAME bucket_key string - bucket_key collision across profiles is
    possible (it is built purely from normalized spec fields, which say
    nothing about which profile produced them) and must not be assumed
    away. Item ids are kept globally unique across the two profiles on
    purpose - record_sighting()'s existence check is on item_id alone
    (design.md §16 P6's separately-scoped, NOT-touched-here prerequisite),
    so two profiles sharing an item_id would silently collapse into one
    row before this test ever got to the code path it's checking.

    Prices are chosen so a mixed pool gives a THIRD, visibly wrong p50 -
    not merely "a different number from one side," which a boundary
    coincidence could produce by accident, but a value that matches
    neither profile's own real percentile at all.
    """
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    bucket_key = "1|intel-10th|16|256"  # identical string for both profiles

    # thinkpad-t14: five fast candidates at $100-$140. Real p50 = $120.00.
    a_prices = [10000, 11000, 12000, 13000, 14000]
    for i, price in enumerate(a_prices):
        item_id = f"a-item-{i}"
        sight(conn, item_id, t0 + i, price_cents=price, profile_id=PROFILE_ID)
        set_spec(conn, item_id, bucket_key)
        mark_gone(conn, item_id, t0 + i + 600)

    # other-profile: five fast candidates at $500-$540. Real p50 = $520.00.
    b_prices = [50000, 51000, 52000, 53000, 54000]
    for i, price in enumerate(b_prices):
        item_id = f"b-item-{i}"
        sight(conn, item_id, t0 + i, price_cents=price, profile_id=OTHER_PROFILE_ID)
        set_spec(conn, item_id, bucket_key)
        mark_gone(conn, item_id, t0 + i + 600)

    a_candidates = derive_candidates(conn, profile_id=PROFILE_ID)
    b_candidates = derive_candidates(conn, profile_id=OTHER_PROFILE_ID)

    assert {c.item_id for c in a_candidates} == {f"a-item-{i}" for i in range(5)}
    assert {c.item_id for c in b_candidates} == {f"b-item-{i}" for i in range(5)}
    assert sorted(c.price_cents for c in a_candidates) == a_prices
    assert sorted(c.price_cents for c in b_candidates) == b_prices

    a_baselines = compute_baselines(a_candidates, fast_lifespan_hours=24, min_samples=5)
    b_baselines = compute_baselines(b_candidates, fast_lifespan_hours=24, min_samples=5)
    assert len(a_baselines) == 1 and a_baselines[0].bucket_key == bucket_key
    assert len(b_baselines) == 1 and b_baselines[0].bucket_key == bucket_key
    assert a_baselines[0].p50_cents == 12000  # $120.00 - A alone
    assert b_baselines[0].p50_cents == 52000  # $520.00 - B alone

    # A pooled derivation (what the pre-fix code did) would see all ten
    # candidates under the one shared bucket_key and compute p50 over the
    # combined ten-value list: index ceil(0.5*10)-1 = 4 -> 14000 ($140.00),
    # a THIRD value distinct from either profile's own $120.00 or $520.00 -
    # this is the number that must never appear on either side.
    pooled_prices = sorted(a_prices + b_prices)
    pooled_p50 = nearest_rank_percentile(pooled_prices, 50)
    assert pooled_p50 == 14000
    assert a_baselines[0].p50_cents != pooled_p50
    assert b_baselines[0].p50_cents != pooled_p50


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
# group_fast_candidates_by_bucket() - shared by compute_baselines() and
# reporting/panels.py's baseline_queue() (V0.11b Part A)
# ---------------------------------------------------------------------------


def test_group_fast_candidates_by_bucket_excludes_slow_ones(tmp_path):
    conn = make_conn(tmp_path)
    t0 = 1_000_000
    sight(conn, "fast-1", t0, price_cents=10000)
    set_spec(conn, "fast-1", BUCKET)
    mark_gone(conn, "fast-1", t0 + 3600)  # 1h - fast at a 24h threshold

    sight(conn, "slow-1", t0, price_cents=20000)
    set_spec(conn, "slow-1", BUCKET)
    mark_gone(conn, "slow-1", t0 + 100 * 3600)  # 100h - slow

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)
    by_bucket = group_fast_candidates_by_bucket(candidates, fast_lifespan_hours=24)

    assert list(by_bucket.keys()) == [BUCKET]
    assert [c.item_id for c in by_bucket[BUCKET]] == ["fast-1"]


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

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)
    assert len(candidates) == 11
    assert compute_baselines(candidates, fast_lifespan_hours=24, min_samples=12) == []

    sight(conn, "item-11", 1_000_000, price_cents=10000 + 11 * 100)
    set_spec(conn, "item-11", BUCKET)
    mark_gone(conn, "item-11", 1_000_000 + 3600)

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)
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

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)
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

    candidates = derive_candidates(conn, profile_id=PROFILE_ID)
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
