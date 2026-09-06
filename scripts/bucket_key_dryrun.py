#!/usr/bin/env python3
"""Read-only measurement of what bucket_key: [generation, cpu_family,
ram_tier] (three fields) would produce versus the current
[generation, cpu_family, ram_tier, storage_tier] (four fields).

    python scripts/bucket_key_dryrun.py --profile profiles/thinkpad-t14.yaml \
        [--db data/dealwatch.db]

This is measurement only, for a decision that has NOT been made. It does
not write to the database (mode=ro connection) and it does not change
profiles/thinkpad-t14.yaml's bucket_key. Dropping storage_tier from
bucket_key - if it happens at all - is a separate later milestone with its
own backfill; see CLAUDE.md's open items.

Deliberately does NOT call baselines.derive_candidates(): that function
reads the STORED bucket_key and drops any key containing '?' before this
script gets a chance to re-key the listing under a different field list -
which is exactly the population this report exists to measure. Instead it
re-derives the same candidate pool baselines._derive() does, using the
exact same helpers (select_price, _LAST_OBSERVATION) and the same
exclusion order, EXCEPT the '?' filter - that's applied afterward, once
per field-list composition, not once for a hardcoded four-field key.

SELF-CHECK (see verify_against_derive_candidates): re-runs the derivation
with the profile's ACTUAL bucket_key field list and asserts the resulting
candidate set is byte-for-byte identical to baselines.derive_candidates()
after filtering out '?' keys. This is what proves the numbers below
describe the real pipeline rather than a parallel reimplementation that
has quietly drifted from it. It runs every time this script runs, not as
a separate test - a dry run whose own self-check didn't pass would be
worse than no dry run at all.
"""

import argparse
import json
import logging
import sqlite3
from dataclasses import dataclass

from dealwatch.engine import baselines
from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import _build_bucket_key

logger = logging.getLogger(__name__)

# Same pool baselines._DEAD_OK_LISTINGS selects, plus spec_json - we need
# the spec to re-key under a different field list, which the stored
# bucket_key alone can't give us. Built by extending the real query text
# rather than copying it by hand, so a future change to that query can't
# silently leave this script selecting a different pool than the real
# pipeline does - the assert is what makes "silently" impossible instead
# of just unlikely.
_DRY_RUN_LISTINGS = baselines._DEAD_OK_LISTINGS.replace(
    "SELECT item_id, bucket_key, gone_at, first_seen, last_seen",
    "SELECT item_id, bucket_key, gone_at, first_seen, last_seen, spec_json",
)
assert "spec_json" in _DRY_RUN_LISTINGS and _DRY_RUN_LISTINGS != baselines._DEAD_OK_LISTINGS, (
    "baselines._DEAD_OK_LISTINGS's column list changed shape - "
    "update the .replace() above to match"
)

FOUR_FIELD = ["generation", "cpu_family", "ram_tier", "storage_tier"]
THREE_FIELD = ["generation", "cpu_family", "ram_tier"]

_TOP_N = 10


def open_readonly(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@dataclass
class PreBucketCandidate:
    """Everything about a candidate that does NOT depend on which fields
    bucket_key is built from - the same shape as baselines.LifespanCandidate
    minus bucket_key itself, which is computed per field-list composition
    downstream (see rekey_candidates)."""

    item_id: str
    spec: dict
    price_cents: int
    price_is_price_only: bool
    lifespan_seconds: int


def derive_pre_bucket_candidates(conn) -> list[PreBucketCandidate]:
    """Same exclusions as baselines._derive(), in the same order, EXCEPT
    the '?' filter - deliberately omitted, since which keys contain '?'
    depends on the field list being measured, not on some fixed pipeline
    fact. Uses baselines.select_price and baselines._LAST_OBSERVATION
    directly rather than reimplementing either.
    """
    rows = conn.execute(_DRY_RUN_LISTINGS).fetchall()
    candidates: list[PreBucketCandidate] = []

    for row in rows:
        if row["first_seen"] == row["last_seen"]:
            continue  # never confirmed by a sweep - see baselines.py

        if row["bucket_key"] is None:
            continue  # matches baselines._derive()'s defensive NULL check

        last_obs = conn.execute(baselines._LAST_OBSERVATION, (row["item_id"],)).fetchone()
        if last_obs is None:
            continue

        selected = baselines.select_price(last_obs["total_cents"], last_obs["price_cents"])
        if selected is None:
            continue  # auction row with no usable price at all
        price_cents, price_is_price_only = selected

        lifespan_seconds = row["gone_at"] - last_obs["observed_at"]
        if lifespan_seconds < 0:
            logger.warning(
                "item %s has a negative lifespan (gone_at=%s < observed_at=%s) - dropping",
                row["item_id"], row["gone_at"], last_obs["observed_at"],
            )
            continue

        spec = json.loads(row["spec_json"]) if row["spec_json"] else {}
        candidates.append(
            PreBucketCandidate(
                item_id=row["item_id"],
                spec=spec,
                price_cents=price_cents,
                price_is_price_only=price_is_price_only,
                lifespan_seconds=lifespan_seconds,
            )
        )

    return candidates


def rekey_candidates(
    pre_bucket: list[PreBucketCandidate], fields: list[str]
) -> list[baselines.LifespanCandidate]:
    """Build a bucket_key per candidate for one field-list composition,
    using engine._build_bucket_key directly (the same '?'-for-None, '|'-
    join convention the real pipeline uses) rather than reimplementing the
    join. Includes candidates whose key contains '?' - callers split
    clean-vs-'?' themselves, since what counts as "clean" is exactly what
    varies between the two compositions being compared here."""
    return [
        baselines.LifespanCandidate(
            item_id=c.item_id,
            bucket_key=_build_bucket_key(fields, c.spec),
            price_cents=c.price_cents,
            price_is_price_only=c.price_is_price_only,
            lifespan_seconds=c.lifespan_seconds,
        )
        for c in pre_bucket
    ]


def verify_against_derive_candidates(conn, pre_bucket: list[PreBucketCandidate], profile) -> None:
    """The required self-check: re-derive under the profile's ACTUAL
    bucket_key field list, drop '?' keys, and assert the result is
    identical (same item_ids, prices, lifespans, bucket_keys) to
    baselines.derive_candidates() - the real pipeline's own answer. Exits
    non-zero on any discrepancy rather than letting the rest of the report
    run against numbers that don't describe the real pipeline.
    """
    rekeyed = rekey_candidates(pre_bucket, profile.bucket_key)
    dry_run_clean = {
        (c.item_id, c.bucket_key, c.price_cents, c.price_is_price_only, c.lifespan_seconds)
        for c in rekeyed
        if "?" not in c.bucket_key
    }

    real = baselines.derive_candidates(conn)
    real_set = {
        (c.item_id, c.bucket_key, c.price_cents, c.price_is_price_only, c.lifespan_seconds)
        for c in real
    }

    if dry_run_clean != real_set:
        only_in_dry_run = dry_run_clean - real_set
        only_in_real = real_set - dry_run_clean
        print("SELF-CHECK FAILED: dry-run derivation does not match baselines.derive_candidates()")
        print(f"  {len(only_in_dry_run)} candidate(s) only in the dry run:")
        for c in sorted(only_in_dry_run)[:20]:
            print(f"    {c}")
        print(f"  {len(only_in_real)} candidate(s) only in derive_candidates():")
        for c in sorted(only_in_real)[:20]:
            print(f"    {c}")
        raise SystemExit(1)


def _fast_counts_by_bucket(
    clean_candidates: list[baselines.LifespanCandidate], threshold_seconds: int
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in clean_candidates:
        if c.lifespan_seconds < threshold_seconds:
            counts[c.bucket_key] = counts.get(c.bucket_key, 0) + 1
    return counts


def _report_composition(
    label: str,
    pre_bucket: list[PreBucketCandidate],
    fields: list[str],
    *,
    fast_lifespan_hours: int,
    min_samples: int,
) -> str:
    rekeyed = rekey_candidates(pre_bucket, fields)
    clean = [c for c in rekeyed if "?" not in c.bucket_key]
    has_qmark = [c for c in rekeyed if "?" in c.bucket_key]

    lines = [f"=== {label}: bucket_key = {fields} ==="]
    lines.append(
        f"  candidates after exclusions: {len(rekeyed)} total "
        f"({len(clean)} clean, {len(has_qmark)} contain '?')"
    )
    lines.append(
        f"  distinct buckets: {len({c.bucket_key for c in clean})} clean, "
        f"{len({c.bucket_key for c in rekeyed})} total including '?'"
    )

    baselines_list = baselines.compute_baselines(
        clean, fast_lifespan_hours=fast_lifespan_hours, min_samples=min_samples
    )
    lines.append(f"  buckets clearing min_samples={min_samples} (fast population): {len(baselines_list)}")
    for b in sorted(baselines_list, key=lambda b: -b.n):
        lines.append(
            f"    {b.bucket_key}: n={b.n} p25=${b.p25_cents / 100:.2f} p50=${b.p50_cents / 100:.2f}"
        )

    threshold_seconds = fast_lifespan_hours * 3600
    fast_counts = _fast_counts_by_bucket(clean, threshold_seconds)
    top = sorted(fast_counts.items(), key=lambda kv: -kv[1])[:_TOP_N]
    lines.append(f"  top {_TOP_N} buckets by fast-population size:")
    for bucket_key, n in top:
        qualifies = " QUALIFIES" if n >= min_samples else ""
        lines.append(f"    {bucket_key}: n={n}{qualifies}")

    return "\n".join(lines)


def _attribution_table(pre_bucket: list[PreBucketCandidate]) -> str:
    """Of the candidates whose 4-field key contains '?', why: storage_tier
    only, ram_tier only, generation-or-cpu_family only, or more than one
    field. Mutually exclusive and exhaustive over the has-any-'?'
    population - this is the number that decides whether dropping
    storage_tier is worth a full backfill."""
    storage_only = 0
    ram_only = 0
    gen_or_cpu_only = 0
    multiple = 0
    total_qmark = 0

    for c in pre_bucket:
        null_fields = [f for f in FOUR_FIELD if c.spec.get(f) is None]
        if not null_fields:
            continue
        total_qmark += 1
        if len(null_fields) > 1:
            multiple += 1
        elif null_fields[0] == "storage_tier":
            storage_only += 1
        elif null_fields[0] == "ram_tier":
            ram_only += 1
        else:  # generation or cpu_family
            gen_or_cpu_only += 1

    lines = [
        "=== attribution: why does the 4-field key contain '?' ===",
        f"  total with at least one '?': {total_qmark}",
        f"    storage_tier only:          {storage_only}",
        f"    ram_tier only:              {ram_only}",
        f"    generation or cpu_family:   {gen_or_cpu_only}",
        f"    more than one field:        {multiple}",
    ]
    return "\n".join(lines)


def run_dry_run(profile, conn) -> str:
    fast_lifespan_hours = profile.scoring.get("fast_lifespan_hours", 24)
    min_samples = profile.scoring.get("min_samples", 12)

    pre_bucket = derive_pre_bucket_candidates(conn)
    verify_against_derive_candidates(conn, pre_bucket, profile)

    sections = [
        _report_composition(
            "4-field (current)", pre_bucket, FOUR_FIELD,
            fast_lifespan_hours=fast_lifespan_hours, min_samples=min_samples,
        ),
        _report_composition(
            "3-field (storage_tier dropped)", pre_bucket, THREE_FIELD,
            fast_lifespan_hours=fast_lifespan_hours, min_samples=min_samples,
        ),
        _attribution_table(pre_bucket),
    ]
    return "\n\n".join(sections)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="path to a profiles/*.yaml file")
    parser.add_argument("--db", default="data/dealwatch.db")
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)

    conn = open_readonly(args.db)
    try:
        print(run_dry_run(profile, conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
