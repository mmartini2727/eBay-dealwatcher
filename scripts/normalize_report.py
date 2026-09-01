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
from dealwatch.normalize import engine as engine_internals
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


def _generation_from_cpu_family(profile) -> dict:
    """What each cpu_family value implies for `generation`, per the
    profile's own derive rules - built once so the disagreement check below
    doesn't need to re-simulate the whole derive stage per listing. Only
    plain equality rules ({cpu_family: <value>} -> generation) are
    meaningful here; startswith rules target cpu_vendor, not generation."""
    return {
        rule.when["cpu_family"]: rule.value
        for rule in profile.derive
        if rule.field == "generation"
        and set(rule.when) == {"cpu_family"}
        and isinstance(rule.when["cpu_family"], str)
    }


def run_report(profile, conn: sqlite3.Connection, seed: int) -> str:
    compiled = compile_profile(profile)
    generation_extract = compiled.extract.get("generation")
    generation_from_cpu_family = _generation_from_cpu_family(profile)

    mapping_failures = 0
    status_counts: Counter = Counter()
    # Pre-seed every reject rule at 0 hits, not just the ones that fired -
    # a rule that never fires is itself worth seeing (dead rule? condition
    # 7000 just never used by real sellers? wrong pattern?).
    reject_hits: dict[str, list[str]] = defaultdict(list, {r.id: [] for r in profile.reject})
    # ok and partial tracked separately (V0.7a): a bucket reaching
    # min_samples on partial listings alone can never back a baseline -
    # partial listings are, by definition, missing a bucket_require field.
    bucket_counts_ok: Counter = Counter()
    bucket_counts_partial: Counter = Counter()
    partial_examples: list[tuple[str, dict]] = []
    not_target_examples: list[str] = []
    generation_agreements = 0
    generation_disagreements = 0
    disagreement_examples: list[tuple[str, str, str]] = []

    now = datetime.now(timezone.utc)

    for row in conn.execute(LATEST_OBSERVATION_PER_LISTING):
        raw = json.loads(row["raw_json"])
        try:
            listing = map_item_summary(raw, now)
        except ListingMappingError:
            mapping_failures += 1
            continue

        listing_fields = listing.model_dump()
        result = normalize(profile, listing_fields)
        status_counts[result.spec_status] += 1

        if result.spec_status == "rejected":
            reject_hits[result.reject_rule_id].append(listing.title)
        elif result.spec_status == "not_target":
            not_target_examples.append(listing.title)
        else:
            if result.bucket_key is not None:
                if result.spec_status == "ok":
                    bucket_counts_ok[result.bucket_key] += 1
                else:
                    bucket_counts_partial[result.bucket_key] += 1
            if result.spec_status == "partial":
                partial_examples.append((listing.title, result.spec))

            # Compare what the title's own text said (pre-derive - derive
            # only fills nulls, so this is a no-op for null-generation
            # listings and matters only when extraction already had an
            # opinion) against what the CPU model implies. Reaches into
            # engine.py's compiled extract step for one field rather than
            # re-implementing extraction here, so this never drifts out of
            # sync with the real pipeline - normalize() itself is not
            # modified or reordered to expose this.
            if generation_extract is not None:
                extracted_generation = engine_internals._extract_field(
                    generation_extract, listing_fields
                )
                implied_generation = generation_from_cpu_family.get(
                    result.spec.get("cpu_family")
                )
                if extracted_generation is not None and implied_generation is not None:
                    if extracted_generation == implied_generation:
                        generation_agreements += 1
                    else:
                        generation_disagreements += 1
                        if len(disagreement_examples) < 10:
                            disagreement_examples.append(
                                (listing.title, extracted_generation, implied_generation)
                            )

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
    lines.append("=== bucket histogram (ok / partial) ===")
    all_buckets = set(bucket_counts_ok) | set(bucket_counts_partial)
    reaching = sum(1 for c in bucket_counts_ok.values() if c >= min_samples)
    singletons = sum(1 for c in bucket_counts_ok.values() if c == 1)
    lines.append(
        f"  {len(all_buckets)} distinct buckets; {reaching} OK-only buckets reach "
        f"scoring.min_samples={min_samples}; {singletons} OK-only buckets are n=1 "
        f"(partial listings can't back a baseline, so they don't count toward either)"
    )
    for bucket_key in sorted(
        all_buckets,
        key=lambda k: -(bucket_counts_ok.get(k, 0) + bucket_counts_partial.get(k, 0)),
    ):
        ok_n = bucket_counts_ok.get(bucket_key, 0)
        partial_n = bucket_counts_partial.get(bucket_key, 0)
        lines.append(f"    {bucket_key}: {ok_n} ok, {partial_n} partial")

    lines.append("")
    lines.append("=== generation: title text vs. CPU-model-implied (extracted-generation listings only) ===")
    lines.append(
        f"  {generation_agreements} agree, {generation_disagreements} disagree"
    )
    for title, extracted, implied in disagreement_examples:
        lines.append(f"    - extracted={extracted!r} implied={implied!r}: {title}")

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
