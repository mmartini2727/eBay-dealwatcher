"""Survival-derived baseline computation (design.md §2.1, V0.8a).

No scoring, no alerting, no seed_baselines, no sanity floor - this module
only turns dead listings into price/lifespan candidates and turns those
candidates into per-bucket percentiles. See scripts/recompute_baselines.py
for the write job and scripts/baseline_report.py for the report that is
this milestone's actual deliverable - V0.8a is expected to write zero or
near-zero baseline rows against the current database (186 dead 'ok'
listings, zero buckets reaching min_samples), and that is success, not a
bug to chase.

The load-bearing rule, from design.md §2.1 - get this backwards and the
survival signal inverts:

    Lifespan is a property of a PRICE POINT, not a listing. A listing that
    sat at $900 for two hours, was cut to $700, and died 30 hours later is
    TWO price points: one slow ($900, ended by a cut, not a sale) and one
    fast ($700, ended by disappearance). Only the second is evidence about
    selling. Filing the whole ~32h lifespan against $700, or crediting $900
    with any lifespan at all, both manufacture a signal that isn't there.

    Concretely: use only the LAST observation a dead listing ever had.
    Every earlier price point is evidence the price was too high, not
    evidence about how fast anything sells, and is ignored entirely.
"""

import logging
import math
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_DEAD_OK_LISTINGS = """
    SELECT item_id, bucket_key, gone_at
    FROM listings
    WHERE gone_at IS NOT NULL AND spec_status = 'ok'
"""

_LAST_OBSERVATION = """
    SELECT total_cents, price_cents, observed_at
    FROM observations
    WHERE item_id = ?
    ORDER BY id DESC
    LIMIT 1
"""


@dataclass
class LifespanCandidate:
    item_id: str
    bucket_key: str
    price_cents: int
    price_is_price_only: bool  # True if total_cents was NULL and price_cents was used instead
    lifespan_seconds: int


@dataclass
class CandidatePoolStats:
    """The exclusion pipeline's stage-by-stage survivor counts - what
    scripts/baseline_report.py's candidate-pool section prints. Each field
    counts listings surviving up to and including that stage; the last one
    equals len(derive_candidates(conn))."""

    total_dead_ok: int
    has_bucket_key: int
    bucket_key_has_no_question_mark: int
    has_usable_price: int


def _derive(conn) -> tuple[list[LifespanCandidate], CandidatePoolStats]:
    """Shared implementation behind derive_candidates() and
    derive_candidate_pool_stats() - one pass over the data, one place the
    exclusion order (design.md §2.1 / the V0.8a task) is encoded, so the
    report's stage counts can never drift from what the candidates
    themselves actually are.
    """
    rows = conn.execute(_DEAD_OK_LISTINGS).fetchall()
    total_dead_ok = len(rows)
    has_bucket_key = 0
    no_question_mark = 0
    has_usable_price = 0
    candidates: list[LifespanCandidate] = []

    for row in rows:
        bucket_key = row["bucket_key"]
        if bucket_key is None:
            continue
        has_bucket_key += 1

        if "?" in bucket_key:
            continue
        no_question_mark += 1

        last_obs = conn.execute(_LAST_OBSERVATION, (row["item_id"],)).fetchone()
        if last_obs is None:
            # Shouldn't happen - record_sighting always inserts an
            # observation alongside a fresh listing row - but this module
            # doesn't own that guarantee, so it's checked, not assumed.
            continue

        if last_obs["total_cents"] is not None:
            price_cents = last_obs["total_cents"]
            price_is_price_only = False
        elif last_obs["price_cents"] is not None:
            price_cents = last_obs["price_cents"]
            price_is_price_only = True
        else:
            continue  # auction row with no usable price at all
        has_usable_price += 1

        lifespan_seconds = row["gone_at"] - last_obs["observed_at"]
        if lifespan_seconds < 0:
            logger.warning(
                "item %s has a negative lifespan (gone_at=%s < observed_at=%s) - dropping",
                row["item_id"], row["gone_at"], last_obs["observed_at"],
            )
            continue

        candidates.append(
            LifespanCandidate(
                item_id=row["item_id"],
                bucket_key=bucket_key,
                price_cents=price_cents,
                price_is_price_only=price_is_price_only,
                lifespan_seconds=lifespan_seconds,
            )
        )

    stats = CandidatePoolStats(
        total_dead_ok=total_dead_ok,
        has_bucket_key=has_bucket_key,
        bucket_key_has_no_question_mark=no_question_mark,
        has_usable_price=has_usable_price,
    )
    return candidates, stats


def derive_candidates(conn) -> list[LifespanCandidate]:
    """One candidate per dead, cleanly-bucketed, priced listing, built from
    its LAST observation only (see module docstring for why). Pure aside
    from taking an already-open sqlite3.Connection as its first argument,
    matching record_sighting's convention (CLAUDE.md) - no writes, no
    normalize() calls, no profile argument, because bucket_key/spec_status
    are already what the engine decided them to be.
    """
    candidates, _ = _derive(conn)
    return candidates


def derive_candidate_pool_stats(conn) -> CandidatePoolStats:
    """Same derivation as derive_candidates(), broken down by exclusion
    stage - scripts/baseline_report.py section (a)."""
    _, stats = _derive(conn)
    return stats


def nearest_rank_percentile(sorted_values: list[int], pct: float) -> int:
    """Nearest-rank percentile, no interpolation: index = ceil(pct/100 * n)
    - 1, clamped to [0, n-1]. sorted_values must already be sorted
    ascending and non-empty."""
    n = len(sorted_values)
    index = math.ceil(pct / 100 * n) - 1
    index = max(0, min(index, n - 1))
    return sorted_values[index]


@dataclass
class Baseline:
    bucket_key: str
    n: int
    n_price_only: int
    p10_cents: int
    p25_cents: int
    p50_cents: int
    fast_hours: int


def compute_baselines(
    candidates: list[LifespanCandidate],
    *,
    fast_lifespan_hours: int,
    min_samples: int,
) -> list[Baseline]:
    """Per bucket_key: keep candidates faster than fast_lifespan_hours,
    require at least min_samples of them (min_samples applies to the FAST
    population, not the dead population - a bucket can have 40 dead
    listings and 3 fast ones), then compute p10/p25/p50 over their prices.
    """
    threshold_seconds = fast_lifespan_hours * 3600
    by_bucket: dict[str, list[LifespanCandidate]] = {}
    for candidate in candidates:
        if candidate.lifespan_seconds < threshold_seconds:
            by_bucket.setdefault(candidate.bucket_key, []).append(candidate)

    baselines = []
    for bucket_key, fast in by_bucket.items():
        if len(fast) < min_samples:
            continue
        prices = sorted(c.price_cents for c in fast)
        n_price_only = sum(1 for c in fast if c.price_is_price_only)
        baselines.append(
            Baseline(
                bucket_key=bucket_key,
                n=len(fast),
                n_price_only=n_price_only,
                p10_cents=nearest_rank_percentile(prices, 10),
                p25_cents=nearest_rank_percentile(prices, 25),
                p50_cents=nearest_rank_percentile(prices, 50),
                fast_hours=fast_lifespan_hours,
            )
        )
    return baselines
