#!/usr/bin/env python3
"""Repair false gone_at marks caused by the pre-V0.7c pagination gap.

    python scripts/repair_false_gone.py --db data/dealwatch.db [--dry-run]

The old sweep ceiling (100 x 10 = 1,000) was smaller than the active set
(~1,013 and growing), so listings past the pagination horizon were never
seen by any sweep and got marked gone on the miss_count timer even though
they were still live on eBay - see CLAUDE.md's Traps and the V0.7c task.

A listing with last_seen == first_seen was never confirmed present by a
single sweep after it was first recorded - its gone_at is not evidence of
anything, real or false. This clears gone_at, lifespan_mins, and
miss_count on exactly those rows. Anything genuinely gone will be
re-marked within three sweeps by the corrected collector; leaving
known-bad rows in the baseline population (they'd all show lifespan 0,
the band the survival baseline weighs most heavily) is worse than
re-deriving them.

Writes. Idempotent: a second run selects zero rows, because clearing
gone_at is exactly what removes a row from the WHERE clause below.
"""

import argparse

from dealwatch.storage.sqlite import connect

# Same "latest observation per listing" idiom as scripts/backfill_item_url.py
# and scripts/baseline_report.py - LEFT JOIN so a listing with zero
# observations still shows up (price_cents NULL) rather than vanishing.
_TARGET_ROWS = """
    SELECT l.item_id, l.title, l.first_seen, latest.price_cents
    FROM listings l
    LEFT JOIN (
        SELECT o.item_id, o.price_cents
        FROM observations o
        JOIN (
            SELECT item_id, MAX(id) AS latest_id FROM observations GROUP BY item_id
        ) m ON o.item_id = m.item_id AND o.id = m.latest_id
    ) latest ON latest.item_id = l.item_id
    WHERE l.gone_at IS NOT NULL AND l.last_seen = l.first_seen
"""

_CLEAR = """
    UPDATE listings
    SET gone_at = NULL, lifespan_mins = NULL, miss_count = 0
    WHERE gone_at IS NOT NULL AND last_seen = first_seen
"""


def run_repair(conn, *, dry_run: bool) -> str:
    rows = conn.execute(_TARGET_ROWS).fetchall()

    if dry_run:
        sample = rows[:10]
        lines = [f"{len(rows)} row(s) would be cleared (dry run, no writes)"]
        for row in sample:
            lines.append(
                f"  {row['item_id']}  ${(row['price_cents'] or 0) / 100:.2f}  "
                f"first_seen={row['first_seen']}  {row['title']}"
            )
        return "\n".join(lines)

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(_CLEAR)
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise

    return f"{len(rows)} row(s) cleared"


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
        print(run_repair(conn, dry_run=args.dry_run))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
