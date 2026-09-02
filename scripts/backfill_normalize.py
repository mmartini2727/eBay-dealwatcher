#!/usr/bin/env python3
"""Backfill: re-normalize listings whose spec was never computed, or is
stale (V0.7b).

    python scripts/backfill_normalize.py --db data/dealwatch.db \
        --profile profiles/thinkpad-t14.yaml [--all]

Writes. Opens the database read-write (dealwatch.storage.sqlite.connect) -
unlike scripts/normalize_report.py's mode=ro. Point this at the live file
knowing it will update spec_json/bucket_key/spec_status/reject_rule_id on
every row it selects; point it at a VACUUM INTO snapshot instead if you
just want to look without changing anything.

By default, selects listings where spec_status IN ('pending', 'stale') -
the two states that mean "never normalized" and "was normalized, but the
title has changed since" (design.md §4.1). --all re-normalizes every
listing regardless of current status, for use after a profile/regex change
whose effects need to replace a previous run's results wholesale.

For each selected listing, reads its most recent observation's raw_json,
maps it with map_item_summary(), runs normalize(), and calls store_spec() -
the same function dealwatch.engine.collector calls inline on every new
sighting. One implementation, two callers: a listing normalized by the
live collector and one normalized by this backfill can never compute a
spec_json differently just because they went through different code paths.

Idempotent: normalize() and store_spec() are both pure functions of their
inputs, so running this twice with no data changes in between produces
identical rows both times. With the default filter, the second run also
simply selects nothing (nothing is left pending/stale).

A listing with no observations at all, or whose latest raw_json fails to
map, is left completely untouched - not even a status flip - and counted
separately in the summary. Never skipped silently.
"""

import argparse
import json
from collections import Counter
from datetime import datetime, timezone

from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import compile_profile, normalize
from dealwatch.normalize.listing import ListingMappingError, map_item_summary
from dealwatch.storage.sqlite import connect, store_spec

# Mirrors scripts/normalize_report.py's LATEST_OBSERVATION_PER_LISTING, plus
# a LEFT JOIN back to listings so a listing with zero observations shows up
# with raw_json NULL instead of vanishing from the result set - that's what
# lets "no observations" be counted rather than silently absent. Kept as a
# second copy rather than imported: normalize_report.py is a standalone
# script, not a package dealwatch code imports from, and importing between
# two sibling scripts for six lines of SQL is more fragile than duplicating
# them.
_TARGET_LISTINGS = """
    SELECT l.item_id, latest.raw_json
    FROM listings l
    LEFT JOIN (
        SELECT o.item_id, o.raw_json
        FROM observations o
        JOIN (
            SELECT item_id, MAX(id) AS latest_id FROM observations GROUP BY item_id
        ) m ON o.item_id = m.item_id AND o.id = m.latest_id
    ) latest ON latest.item_id = l.item_id
"""

_DEFAULT_FILTER = " WHERE l.spec_status IN ('pending', 'stale')"


def run_backfill(profile, conn, *, all_listings: bool) -> str:
    compile_profile(profile)  # fail fast on a bad profile before touching any row

    query = _TARGET_LISTINGS if all_listings else _TARGET_LISTINGS + _DEFAULT_FILTER
    rows = conn.execute(query).fetchall()

    status_counts: Counter = Counter()
    no_observations = 0
    mapping_failures = 0
    now = datetime.now(timezone.utc)

    for row in rows:
        if row["raw_json"] is None:
            no_observations += 1
            continue

        raw = json.loads(row["raw_json"])
        try:
            listing = map_item_summary(raw, now)
        except ListingMappingError:
            mapping_failures += 1
            continue

        result = normalize(profile, listing.model_dump())
        store_spec(conn, row["item_id"], result)
        status_counts[result.spec_status] += 1

    total = sum(status_counts.values())
    lines = ["=== spec_status distribution ==="]
    for status, count in status_counts.most_common():
        pct = (100 * count / total) if total else 0.0
        lines.append(f"  {status:12s} {count:5d}  ({pct:5.1f}%)")
    lines.append(
        f"  ({total} listings normalized and written; {len(rows)} selected, "
        f"{no_observations} had no observations, {mapping_failures} failed "
        f"map_item_summary() - all left untouched)"
    )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument("--profile", required=True, help="path to a profiles/*.yaml file")
    parser.add_argument(
        "--all",
        action="store_true",
        help="re-normalize every listing regardless of spec_status, not just pending/stale",
    )
    args = parser.parse_args(argv)

    profile = load_profile(args.profile)

    conn = connect(args.db)  # read-write - see module docstring
    try:
        print(run_backfill(profile, conn, all_listings=args.all))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
