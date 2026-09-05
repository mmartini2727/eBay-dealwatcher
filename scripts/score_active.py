#!/usr/bin/env python3
"""Score every active, normalized listing against the baseline ladder
(design.md §5.6, V0.8b).

    python scripts/score_active.py --profile profiles/thinkpad-t14.yaml \
        [--db data/dealwatch.db] [--limit 20]

Writes: persists sanity_flagged on every listing scored (design.md §5.3 -
the sanity-floor queue has to be queryable later, not a log line). Does
NOT send alerts, decide buyability, or touch the alerts table - that's
V0.9. Not wired into the collector loop or FastAPI this milestone; V0.9
decides where scoring gets called from.

"Active" means gone_at IS NULL; only spec_status='ok' listings are scored
- 'partial' listings have a bucket_key containing '?' and would only ever
resolve to a seed baseline anyway, and this script's job is to surface the
best real deals, not to audit partials (that's normalize_report.py).

Prints the best --limit listings by ratio_to_p25, ascending - the cheapest
relative to their baseline first - with the layer and sample count visible
on every line, because "real deal against 12 real samples" and "seed chart
estimate was wrong" must never look the same at a glance.
"""

import argparse
import json

from dealwatch.engine.baselines import select_price
from dealwatch.engine.collector import load_profile
from dealwatch.engine.scoring import compile_seed_baselines, score_listing
from dealwatch.normalize.engine import compile_profile
from dealwatch.storage.sqlite import connect, get_latest_observation, store_sanity_flag

_ACTIVE_OK_LISTINGS = """
    SELECT item_id, bucket_key, spec_json, item_web_url
    FROM listings
    WHERE profile_id = ? AND gone_at IS NULL AND spec_status = 'ok'
"""


def run_score_active(profile, conn, *, limit: int) -> str:
    compile_profile(profile)  # fail fast on a bad profile before scoring anything
    compiled_seeds = compile_seed_baselines(profile)

    rows = conn.execute(_ACTIVE_OK_LISTINGS, (profile.id,)).fetchall()

    results = []
    skipped_no_price = 0
    for row in rows:
        observation = get_latest_observation(conn, row["item_id"])
        selected = select_price(
            observation["total_cents"] if observation else None,
            observation["price_cents"] if observation else None,
        )
        if selected is None:
            skipped_no_price += 1
            continue
        price_cents, price_is_price_only = selected

        spec = json.loads(row["spec_json"]) if row["spec_json"] else {}
        result = score_listing(
            conn,
            profile,
            compiled_seeds,
            item_id=row["item_id"],
            bucket_key=row["bucket_key"],
            spec=spec,
            price_cents=price_cents,
            price_is_price_only=price_is_price_only,
            item_web_url=row["item_web_url"],
        )
        store_sanity_flag(conn, row["item_id"], result.sanity_flagged)
        results.append(result)

    results.sort(key=lambda r: r.ratio_to_p25)

    lines = [
        f"{len(results)} listing(s) scored, {skipped_no_price} skipped (no usable price)",
        "",
    ]
    for r in results[:limit]:
        flag = " SANITY-FLOOR" if r.sanity_flagged else ""
        price_note = " (price only)" if r.price_is_price_only else ""
        lines.append(
            f"  ratio_p25={r.ratio_to_p25:5.2f}  ratio_p50={r.ratio_to_p50:5.2f}  "
            f"${r.price_cents / 100:7.2f}{price_note}  "
            f"[{r.baseline_layer:8s} n={r.baseline_n if r.baseline_n is not None else '-'}]  "
            f"{r.bucket_key or '(no bucket)'}  {r.item_id}{flag}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="path to a profiles/*.yaml file")
    parser.add_argument("--db", default="data/dealwatch.db")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)

    conn = connect(args.db)  # read-write - persists sanity_flagged
    try:
        print(run_score_active(profile, conn, limit=args.limit))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
