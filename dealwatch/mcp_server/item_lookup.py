"""Resolve explain_listing()'s item_id_or_url argument into zero or more
stored item_id values (design.md §15 tool 4). A full item_id is
unambiguous by construction; a bare legacy eBay item number can match more
than one stored row when variations exist (item_id shaped
v1|<number>|<variation>, storage/sqlite.py's migration 5 comment) -
resolving candidates is this module's only job. Deciding what to do with
zero or multiple matches is explain_listing()'s.
"""

import re
import sqlite3

# eBay item URLs put the legacy item number as the last path segment
# before any query string, e.g. https://www.ebay.com/itm/<slug>/<number>
# or https://www.ebay.com/itm/<number>?hash=... - matched generically
# (9-15 digits, eBay's real legacy item numbers) rather than tied to one
# exact URL shape, since eBay has used more than one itm/ URL format.
_URL_NUMBER = re.compile(r"(\d{9,15})(?:[/?#]|$)")
_BARE_NUMBER = re.compile(r"^\d+$")


def looks_like_item_id(value: str) -> bool:
    """A full Browse API item_id has the form v1|<number>|<variation-or-0>
    (or the 2-part v1|<number> shape storage/sqlite.py's migration 5
    comment also handles) - never bare digits, never a URL."""
    return value.startswith("v1|")


def extract_item_number_from_url(url: str) -> str | None:
    """The LAST long digit run in the URL, not the first - a category id
    or other numeric path segment earlier in an eBay URL is also
    all-digits, and the item number is always the final one."""
    matches = _URL_NUMBER.findall(url)
    return matches[-1] if matches else None


def resolve_item_ids(
    conn: sqlite3.Connection, profile_id: str, item_id_or_url: str
) -> list[str]:
    """Zero, one, or many candidate item_ids for item_id_or_url:
      - a full item_id (`v1|...`) resolves to itself, unchecked against the
        database here - explain_listing() looks it up and reports
        found: false if it doesn't exist, same as any other not-found case.
      - a URL is reduced to its legacy item number, then resolved as below.
        An unrecognized URL shape (no long digit run) resolves to no
        candidates.
      - a bare legacy number matches every stored item_id shaped
        `v1|<number>` or `v1|<number>|<anything>` for this profile -
        possibly more than one row, when variations exist.
      - anything else (not an item_id, not a URL, not all digits) resolves
        to no candidates.

    conn must have row_factory unset or a plain-tuple-compatible one - this
    reads column 0 positionally, matching every other module's contract in
    this codebase.
    """
    value = item_id_or_url.strip()
    if looks_like_item_id(value):
        return [value]

    if "://" in value or "ebay." in value:
        number = extract_item_number_from_url(value)
        if number is None:
            return []
    elif _BARE_NUMBER.match(value):
        number = value
    else:
        return []

    # `number` is guaranteed all-digits (either _BARE_NUMBER-matched or
    # extracted via _URL_NUMBER's own \d{9,15}), so it can never contain a
    # LIKE wildcard character - safe to interpolate into the pattern
    # despite being passed as a bound parameter either way.
    rows = conn.execute(
        "SELECT item_id FROM listings WHERE profile_id = ? "
        "AND (item_id = ? OR item_id LIKE ?)",
        (profile_id, f"v1|{number}", f"v1|{number}|%"),
    ).fetchall()
    return [row[0] for row in rows]
