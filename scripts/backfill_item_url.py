#!/usr/bin/env python3
"""Backfill listings.item_web_url from each listing's latest raw_json
(V0.8a). item_web_url exists today only inside raw_json; V0.9's Discord
alerts need it as a real column rather than re-parsing raw_json per alert.

    python scripts/backfill_item_url.py [--db data/dealwatch.db]

Writes. Idempotent: a listing whose item_web_url is already set is left
alone entirely - not re-parsed, not re-written to the same value - and
counted as "already set", so running this twice changes nothing on the
second pass. Never raises: a listing with no observations, unparseable
raw_json, or no itemWebUrl in it is counted as "skipped" and left
untouched.
"""

import argparse
import json

from dealwatch.storage.sqlite import connect

# LEFT JOIN, not JOIN: a listing with zero observations (shouldn't happen,
# but not this script's job to assume) must still show up, with raw_json
# NULL, so it's counted as skipped rather than silently vanishing from the
# result set - the same reasoning as scripts/backfill_normalize.py's query.
_LATEST_OBSERVATION_PER_LISTING = """
    SELECT l.item_id, l.item_web_url AS current_url, latest.raw_json
    FROM listings l
    LEFT JOIN (
        SELECT o.item_id, o.raw_json
        FROM observations o
        JOIN (
            SELECT item_id, MAX(id) AS latest_id FROM observations GROUP BY item_id
        ) m ON o.item_id = m.item_id AND o.id = m.latest_id
    ) latest ON latest.item_id = l.item_id
"""


def run_backfill_item_url(conn) -> str:
    updated = 0
    skipped = 0
    already_set = 0

    for row in conn.execute(_LATEST_OBSERVATION_PER_LISTING).fetchall():
        if row["current_url"] is not None:
            already_set += 1
            continue

        try:
            raw = json.loads(row["raw_json"])
        except (TypeError, ValueError):
            skipped += 1
            continue

        url = raw.get("itemWebUrl")
        if not url:
            skipped += 1
            continue

        conn.execute(
            "UPDATE listings SET item_web_url = ? WHERE item_id = ?",
            (url, row["item_id"]),
        )
        updated += 1

    return f"updated={updated} skipped={skipped} already_set={already_set}"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/dealwatch.db")
    args = parser.parse_args(argv)

    conn = connect(args.db)  # read-write
    try:
        print(run_backfill_item_url(conn))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
