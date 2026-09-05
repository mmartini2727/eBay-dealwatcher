"""Deal scoring (design.md §5.6, V0.8b): given one active listing, resolve
a baseline via the fallback ladder and score its price against it.

No alerting, no buyability rules (soldered RAM, minimum specs - V0.9, and
they must be YAML-driven when they land, not Python that knows what a
laptop is), no best_offer_weight (dead config - the search filter is
FIXED_PRICE-only and most surviving listings carry BEST_OFFER anyway, so
it would be a near-constant multiplier dressed up as a signal; deleted
from the profile in this milestone rather than left to look live).

The ladder, in order, with no bucket-coarsening step in between:

    1. The survival-derived baselines table row for this bucket_key, if
       one exists (design.md §2.1, V0.8a).
    2. The best-matching seed_baselines entry (most matched `match` keys
       wins; ties break by file order).

A bucket_key containing '?' can never hit layer 1 - V0.8a excludes those
from baseline computation entirely - and always falls through to layer 2.
That's intended, not a gap: partial seed matching already plays the role a
bucket-coarsening step would, with hand-authored numbers instead of a
wider, noisier pool.
"""

from dataclasses import dataclass
from typing import Any

from dealwatch.normalize.engine import ProfileCompileError
from dealwatch.normalize.schema import Profile

BASELINE_LAYER_COMPUTED = "computed"
BASELINE_LAYER_SEED = "seed"

_BASELINE_ROW = """
    SELECT n, p25_cents, p50_cents FROM baselines
    WHERE profile_id = ? AND bucket_key = ?
"""


@dataclass
class CompiledSeedBaseline:
    match: dict[str, Any]
    p25_cents: int
    p50_cents: int


def compile_seed_baselines(profile: Profile) -> list[CompiledSeedBaseline]:
    """Validate profile.seed_baselines and convert p25/p50 from the
    dollars they're authored in to the integer cents everything downstream
    uses. Two entries with an identical `match` block make matching
    ambiguous in a way file order can't honestly resolve - the ambiguity
    is a profile-authoring mistake, not a live-data situation, so it's a
    ProfileCompileError (same as an invalid regex or an unresolvable
    bucket_key field) rather than a silent pick at 2am.

    Callers should run this once at startup, same as compile_profile -
    it's not called automatically by anything, matching that function's
    own precedent (see scripts/backfill_normalize.py's "fail fast" call).
    """
    compiled: list[CompiledSeedBaseline] = []
    seen_matches: list[dict] = []
    for entry in profile.seed_baselines:
        if entry.match in seen_matches:
            raise ProfileCompileError(
                f"seed_baselines: duplicate match block {entry.match!r}"
            )
        seen_matches.append(entry.match)
        compiled.append(
            CompiledSeedBaseline(
                match=entry.match,
                p25_cents=round(entry.p25 * 100),
                p50_cents=round(entry.p50 * 100),
            )
        )
    return compiled


def resolve_seed_baseline(
    seeds: list[CompiledSeedBaseline], spec: dict
) -> CompiledSeedBaseline | None:
    """Most-matched-keys wins; ties break by file order (strict `>`, not
    `>=`, below is what makes the first-seen entry win a tie rather than
    the last). An empty `match` block matches everything with a score of
    0, so it only wins when nothing more specific does - exactly the
    fallback role it's authored for."""
    best: CompiledSeedBaseline | None = None
    best_score = -1
    for entry in seeds:
        if all(spec.get(k) == v for k, v in entry.match.items()):
            score = len(entry.match)
            if score > best_score:
                best = entry
                best_score = score
    return best


@dataclass
class ScoreResult:
    item_id: str
    bucket_key: str | None
    price_cents: int
    price_is_price_only: bool
    baseline_layer: str  # 'computed' | 'seed'
    baseline_match: str  # bucket_key for computed; the seed's match dict, rendered, for seed
    baseline_n: int | None  # sample count for computed; None for seed - there is no n
    baseline_p25_cents: int
    baseline_p50_cents: int
    ratio_to_p25: float
    ratio_to_p50: float
    sanity_flagged: bool
    item_web_url: str | None


def score_listing(
    conn,
    profile: Profile,
    compiled_seeds: list[CompiledSeedBaseline],
    *,
    item_id: str,
    bucket_key: str | None,
    spec: dict,
    price_cents: int,
    price_is_price_only: bool,
    item_web_url: str | None,
) -> ScoreResult:
    """Resolve a baseline for one listing via the fallback ladder and score
    its price against it. price_cents/price_is_price_only are the caller's
    responsibility to derive (dealwatch.engine.baselines.select_price) from
    the listing's latest observation - this function scores whatever price
    it's handed, it doesn't go looking for one.

    baseline_layer and baseline_n are load-bearing for the reader, not
    decoration: without them there is no way to tell "real deal against 12
    real samples" from "seed chart estimate was wrong."
    """
    row = None
    if bucket_key is not None and "?" not in bucket_key:
        row = conn.execute(_BASELINE_ROW, (profile.id, bucket_key)).fetchone()

    if row is not None:
        baseline_layer = BASELINE_LAYER_COMPUTED
        baseline_match = bucket_key
        baseline_n = row["n"]
        p25_cents = row["p25_cents"]
        p50_cents = row["p50_cents"]
    else:
        seed = resolve_seed_baseline(compiled_seeds, spec)
        if seed is None:
            # Only reachable if the profile's seed_baselines has no `{}`
            # fallback entry - a profile-authoring gap, not a listing
            # problem. compile_seed_baselines can't catch this ahead of
            # time without knowing every spec combination a real listing
            # might produce, so it surfaces here instead of silently
            # scoring against nothing.
            raise ValueError(
                f"no baseline available for item {item_id!r} (bucket_key="
                f"{bucket_key!r}) and no matching seed_baselines entry - "
                "does the profile have a `match: {}` fallback?"
            )
        baseline_layer = BASELINE_LAYER_SEED
        baseline_match = str(seed.match)
        baseline_n = None
        p25_cents = seed.p25_cents
        p50_cents = seed.p50_cents

    sanity_floor_pct = profile.scoring.get("sanity_floor_pct", 25)
    # Cross-multiplied rather than (price_cents / p50_cents * 100) <
    # sanity_floor_pct: integer comparison has no rounding behavior to get
    # subtly wrong right at the boundary, and the boundary is exactly what
    # this gate is tested against.
    sanity_flagged = price_cents * 100 < p50_cents * sanity_floor_pct

    return ScoreResult(
        item_id=item_id,
        bucket_key=bucket_key,
        price_cents=price_cents,
        price_is_price_only=price_is_price_only,
        baseline_layer=baseline_layer,
        baseline_match=baseline_match,
        baseline_n=baseline_n,
        baseline_p25_cents=p25_cents,
        baseline_p50_cents=p50_cents,
        ratio_to_p25=price_cents / p25_cents,
        ratio_to_p50=price_cents / p50_cents,
        sanity_flagged=sanity_flagged,
        item_web_url=item_web_url,
    )
