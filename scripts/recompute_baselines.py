#!/usr/bin/env python3
"""Recompute survival-derived baselines (design.md §2.1, V0.8a).

    python scripts/recompute_baselines.py --profile profiles/thinkpad-t14.yaml \
        [--db data/dealwatch.db]

Writes. DELETEs then INSERTs every baselines row for the profile in one
transaction (dealwatch.storage.sqlite.store_baselines) - baselines are
fully recomputable from listings/observations history, so there is no
incremental-update path to get wrong. Manual script only; not wired into
the collector loop or FastAPI this milestone.

No scoring, no alerts. This only derives candidates
(dealwatch.engine.baselines.derive_candidates), computes percentiles over
the fast-lifespan subset per bucket, and writes the result.
scripts/baseline_report.py is the read-only companion that explains WHY a
given run wrote as many (or as few) rows as it did - run that first if the
qualified/skipped counts here are surprising.
"""

import argparse
from datetime import datetime, timezone

from dealwatch.engine.baselines import compute_baselines, derive_candidates
from dealwatch.engine.collector import load_profile
from dealwatch.storage.sqlite import connect, store_baselines


def run_recompute(profile, conn) -> str:
    fast_lifespan_hours = profile.scoring.get("fast_lifespan_hours", 24)
    min_samples = profile.scoring.get("min_samples", 12)

    candidates = derive_candidates(conn)
    baselines = compute_baselines(
        candidates, fast_lifespan_hours=fast_lifespan_hours, min_samples=min_samples
    )
    computed_at = int(datetime.now(timezone.utc).timestamp())
    store_baselines(conn, profile.id, baselines, computed_at)

    fast_bucket_keys = {
        c.bucket_key
        for c in candidates
        if c.lifespan_seconds < fast_lifespan_hours * 3600
    }
    qualified = len(baselines)
    skipped = len(fast_bucket_keys) - qualified
    return (
        f"{qualified} bucket(s) qualified (>= min_samples={min_samples} fast "
        f"candidates at fast_lifespan_hours={fast_lifespan_hours}), {skipped} "
        f"skipped for insufficient samples ({len(candidates)} total dead "
        f"candidates considered)"
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="path to a profiles/*.yaml file")
    parser.add_argument("--db", default="data/dealwatch.db")
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)

    conn = connect(args.db)  # read-write
    try:
        print(run_recompute(profile, conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
