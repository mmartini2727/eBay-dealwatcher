#!/usr/bin/env python3
"""Read-only report over collected listing history (V0.7).

    python scripts/normalize_report.py --profile profiles/thinkpad-t14.yaml \
        [--db data/dealwatch.db] [--seed 42]

Opens the database with mode=ro and never writes anything, ever - not even
implicitly (no journal_mode PRAGMA, no migrations). For each listing, takes
its most recent observation's raw_json, re-maps it with map_item_summary(),
and runs the result through normalize(). This never touches the listings
table's own spec_json/bucket_key/spec_status columns - the engine is pure
and V0.7 is deliberately not on the collector's write path (see
dealwatch/normalize/engine.py's module docstring). The point of this report
is to tell us whether the profile YAML says the right things; the engine's
own test suite already proves it does what the YAML says.
"""

import argparse
import json
import random
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone

from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import compile_profile, normalize
from dealwatch.normalize.listing import ListingMappingError, map_item_summary

LATEST_OBSERVATION_PER_LISTING = """
    SELECT o.item_id, o.raw_json
    FROM observations o
    JOIN (
        SELECT item_id, MAX(id) AS latest_id FROM observations GROUP BY item_id
    ) latest ON o.item_id = latest.item_id AND o.id = latest.latest_id
"""


def open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def run_report(profile, conn: sqlite3.Connection, seed: int) -> str:
    mapping_failures = 0
    status_counts: Counter = Counter()
    # Pre-seed every reject rule at 0 hits, not just the ones that fired -
    # a rule that never fires is itself worth seeing (dead rule? condition
    # 7000 just never used by real sellers? wrong pattern?).
    reject_hits: dict[str, list[str]] = defaultdict(list, {r.id: [] for r in profile.reject})
    bucket_counts: Counter = Counter()
    partial_examples: list[tuple[str, dict]] = []
    not_target_examples: list[str] = []

    now = datetime.now(timezone.utc)

    for row in conn.execute(LATEST_OBSERVATION_PER_LISTING):
        raw = json.loads(row["raw_json"])
        try:
            listing = map_item_summary(raw, now)
        except ListingMappingError:
            mapping_failures += 1
            continue

        result = normalize(profile, listing.model_dump())
        status_counts[result.spec_status] += 1

        if result.spec_status == "rejected":
            reject_hits[result.reject_rule_id].append(listing.title)
        elif result.spec_status == "not_target":
            not_target_examples.append(listing.title)
        else:
            if result.bucket_key is not None:
                bucket_counts[result.bucket_key] += 1
            if result.spec_status == "partial":
                partial_examples.append((listing.title, result.spec))

    total = sum(status_counts.values())
    min_samples = profile.scoring.get("min_samples", 12)
    rng = random.Random(seed)

    lines = []
    lines.append("=== spec_status distribution ===")
    for status, count in status_counts.most_common():
        pct = (100 * count / total) if total else 0.0
        lines.append(f"  {status:12s} {count:5d}  ({pct:5.1f}%)")
    lines.append(
        f"  ({total} listings mapped and normalized; "
        f"{mapping_failures} failed map_item_summary() and were excluded above)"
    )

    lines.append("")
    lines.append("=== reject rule hit counts ===")
    for rule_id, titles in sorted(reject_hits.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  {rule_id}: {len(titles)} hits")
        for title in titles[:3]:
            lines.append(f"    - {title}")

    lines.append("")
    lines.append("=== bucket histogram ===")
    reaching = sum(1 for c in bucket_counts.values() if c >= min_samples)
    singletons = sum(1 for c in bucket_counts.values() if c == 1)
    lines.append(
        f"  {len(bucket_counts)} distinct buckets; {reaching} reach "
        f"scoring.min_samples={min_samples}; {singletons} are n=1"
    )
    for bucket_key, count in bucket_counts.most_common():
        lines.append(f"    {bucket_key}: {count}")

    lines.append("")
    lines.append("=== up to 20 random partial listings (with extracted spec) ===")
    for title, spec in rng.sample(partial_examples, min(20, len(partial_examples))):
        lines.append(f"  - {title}")
        lines.append(f"      spec: {spec}")

    lines.append("")
    lines.append("=== up to 20 random not_target listings ===")
    for title in rng.sample(not_target_examples, min(20, len(not_target_examples))):
        lines.append(f"  - {title}")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="path to a profiles/*.yaml file")
    parser.add_argument("--db", default="data/dealwatch.db")
    parser.add_argument(
        "--seed", type=int, default=42, help="fixed by default so re-runs are comparable"
    )
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)
    compile_profile(profile)  # fail fast on a bad profile before opening the DB

    conn = open_readonly(args.db)
    try:
        print(run_report(profile, conn, args.seed))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
