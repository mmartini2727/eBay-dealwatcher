#!/usr/bin/env python3
"""Baseline candidate-pool report (design.md §2.1, V0.8a). Read-only - this
is the primary deliverable of this milestone, not scripts/recompute_baselines.py.

    python scripts/baseline_report.py --profile profiles/thinkpad-t14.yaml \
        [--db data/dealwatch.db]

mode=ro, like scripts/normalize_report.py - never writes.

Answers four questions, in order:
  (a) How many dead listings are even eligible to vote on a baseline, and
      where does the pool shrink at each exclusion stage?
  (b) How sensitive is that count to the fast-lifespan threshold - would a
      different cutoff meaningfully change how many buckets qualify?
  (c) Per bucket: how many candidates, how many are "fast" at the
      configured threshold, and does it reach min_samples? Near-misses are
      printed, not hidden - a bucket at 10/12 is exactly what this report
      exists to surface.
  (d) FALSIFICATION CHECK: does a fast-selling price actually come in
      lower than a slow one, in the buckets with enough of both to check?
      If not, design.md §2.1's survival premise is wrong and V0.8b should
      not be built on it. Printed plainly either way - this is a check,
      not a sales pitch for the approach.
"""

import argparse
import sqlite3

from dealwatch.engine.baselines import (
    compute_baselines,
    derive_candidate_pool_stats,
    derive_candidates,
    nearest_rank_percentile,
)
from dealwatch.engine.collector import load_profile

_THRESHOLD_HOURS = [2, 6, 24, 72]
_FALSIFICATION_MIN_SIDE = 5


def open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run_report(profile, conn) -> str:
    fast_lifespan_hours = profile.scoring.get("fast_lifespan_hours", 24)
    min_samples = profile.scoring.get("min_samples", 12)
    threshold_seconds = fast_lifespan_hours * 3600

    pool_stats = derive_candidate_pool_stats(conn)
    candidates = derive_candidates(conn)

    by_bucket: dict[str, list] = {}
    for c in candidates:
        by_bucket.setdefault(c.bucket_key, []).append(c)

    lines = []

    lines.append("=== (a) candidate pool ===")
    lines.append(f"  {pool_stats.total_dead_ok} dead listings with spec_status='ok'")
    lines.append(f"  {pool_stats.has_bucket_key} have a bucket_key at all")
    lines.append(f"  {pool_stats.bucket_key_has_no_question_mark} of those have no '?' component")
    lines.append(f"  {pool_stats.has_usable_price} of those have a usable price")
    lines.append(f"  -> {len(candidates)} final candidates")

    lines.append("")
    lines.append("=== (b) threshold sensitivity ===")
    for hours in _THRESHOLD_HOURS:
        hours_seconds = hours * 3600
        fast_count = sum(1 for c in candidates if c.lifespan_seconds < hours_seconds)
        qualifying = compute_baselines(
            candidates, fast_lifespan_hours=hours, min_samples=min_samples
        )
        lines.append(
            f"  {hours:>3}h: {fast_count:4d} candidates qualify as fast, "
            f"{len(qualifying)} bucket(s) reach min_samples={min_samples}"
        )

    lines.append("")
    lines.append(
        f"=== (c) per-bucket breakdown (fast_lifespan_hours={fast_lifespan_hours}, "
        f"min_samples={min_samples}) ==="
    )
    for bucket_key, bucket_candidates in sorted(
        by_bucket.items(), key=lambda kv: -len(kv[1])
    ):
        fast = [c for c in bucket_candidates if c.lifespan_seconds < threshold_seconds]
        n_price_only = sum(1 for c in fast if c.price_is_price_only)
        qualifies = len(fast) >= min_samples
        lines.append(
            f"  {bucket_key}: dead={len(bucket_candidates):3d} "
            f"fast={len(fast)}/{min_samples} price_only={n_price_only} "
            f"{'QUALIFIES' if qualifies else ''}"
        )

    lines.append("")
    lines.append(
        f"=== (d) falsification check: fast (<{fast_lifespan_hours}h) vs. slow price, "
        f"buckets with >={_FALSIFICATION_MIN_SIDE} of each ==="
    )
    fast_cheaper_count = 0
    eligible_buckets = 0
    for bucket_key, bucket_candidates in sorted(by_bucket.items()):
        fast = [c for c in bucket_candidates if c.lifespan_seconds < threshold_seconds]
        slow = [c for c in bucket_candidates if c.lifespan_seconds >= threshold_seconds]
        if len(fast) < _FALSIFICATION_MIN_SIDE or len(slow) < _FALSIFICATION_MIN_SIDE:
            continue
        eligible_buckets += 1
        fast_median = nearest_rank_percentile(sorted(c.price_cents for c in fast), 50)
        slow_median = nearest_rank_percentile(sorted(c.price_cents for c in slow), 50)
        is_cheaper = fast_median < slow_median
        if is_cheaper:
            fast_cheaper_count += 1
        lines.append(
            f"  {bucket_key}: fast n={len(fast)} median=${fast_median / 100:.2f}  "
            f"slow n={len(slow)} median=${slow_median / 100:.2f}  "
            f"{'fast cheaper' if is_cheaper else 'SLOW CHEAPER - premise violated here'}"
        )
    if eligible_buckets == 0:
        lines.append(
            f"  no bucket has >={_FALSIFICATION_MIN_SIDE} fast and "
            f">={_FALSIFICATION_MIN_SIDE} slow candidates - not enough data to check yet"
        )
    lines.append(f"  fast cheaper in {fast_cheaper_count} of {eligible_buckets} buckets")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="path to a profiles/*.yaml file")
    parser.add_argument("--db", default="data/dealwatch.db")
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)

    conn = open_readonly(args.db)
    try:
        print(run_report(profile, conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
