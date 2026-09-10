#!/usr/bin/env python3
"""Backfill lifespan_mins = NULL for listings that were never sweep-confirmed.

    python scripts/backfill_zero_lifespan.py --db data/dealwatch.db [--dry-run]

Pre-V0.9b, the death-marking UPDATE in storage.sqlite.record_sweep wrote
lifespan_mins = (last_seen - first_seen) / 60 unconditionally. A listing
inserted by a fast poll and never confirmed present by any sweep keeps
last_seen == first_seen, so that expression writes 0 - indistinguishable
from "sold in under a minute." engine/baselines.py's derive_candidates()
already excludes exactly these rows from the survival baseline via this
same first_seen == last_seen comparison (V0.8b) - that filter is
unaffected by this script either way. This backfill is for every OTHER
reader of the column: ad-hoc queries, Datasette, the V1.0 MCP server.

Only rows where last_seen = first_seen are touched. A listing that was
genuinely swept and found dead within the same minute also has
lifespan_mins = 0, and that IS a real measurement - it must survive this
backfill untouched, which is why the WHERE clause below checks both
conditions, not lifespan_mins = 0 alone.

Writes. Idempotent: a second run selects zero rows, because clearing
lifespan_mins to NULL is exactly what removes a row from the WHERE clause
below (same idiom as scripts/repair_false_gone.py).
"""

import argparse

from dealwatch.storage.sqlite import connect

_TARGET_ROWS = """
    SELECT item_id, title, first_seen, last_seen
    FROM listings
    WHERE lifespan_mins = 0 AND last_seen = first_seen
"""

_CLEAR = """
    UPDATE listings
    SET lifespan_mins = NULL
    WHERE lifespan_mins = 0 AND last_seen = first_seen
"""


def run_backfill(conn, *, dry_run: bool) -> str:
    rows = conn.execute(_TARGET_ROWS).fetchall()

    if dry_run:
        sample = rows[:10]
        lines = [f"{len(rows)} row(s) would be set to NULL (dry run, no writes)"]
        for row in sample:
            lines.append(
                f"  {row['item_id']}  first_seen={row['first_seen']}  "
                f"last_seen={row['last_seen']}  {row['title']}"
            )
        return "\n".join(lines)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_CLEAR)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    return f"{len(rows)} row(s) set to NULL"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the count and a sample of ten without writing",
    )
    args = parser.parse_args(argv)

    conn = connect(args.db)  # read-write - see module docstring
    try:
        print(run_backfill(conn, dry_run=args.dry_run))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
