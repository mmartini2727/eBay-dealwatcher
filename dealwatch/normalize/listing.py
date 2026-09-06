"""Maps a raw eBay Browse itemSummary dict into one internal Listing shape.

V0.4 scope only: in-memory mapping, nothing else. No persistence (V0.5), no
title parsing into a Spec (V0.7 - see design.md §5.2), no collector (V0.6).
"""

from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel


class ListingMappingError(Exception):
    """Raised when a raw itemSummary is missing item_id, title, or price -
    the only fields a Listing cannot do without."""


class Listing(BaseModel):
    item_id: str
    title: str
    price_cents: int
    seen_at: datetime

    # eBay Browse item_ids for one variation of a multi-variation listing
    # have the form v1|<listing>|<variation> (V0.8d) - which variation
    # appears in a given search result set is unstable, so these are
    # excluded from the survival baseline entirely (engine/baselines.py).
    # See parse_variation_id() below.
    variation_id: str | None = None

    subtitle: str | None = None
    condition_id: int | None = None
    seller: str | None = None
    # Both wanted together: 100% feedback across 3 sales is a very different
    # signal from 99.3% across 40,000 - neither number alone says much.
    # Observations only; no filtering or scoring on these here.
    seller_feedback_pct: float | None = None
    seller_feedback_score: int | None = None
    buying_options: list[str] = []
    item_web_url: str | None = None
    item_location_country: str | None = None
    end_date: datetime | None = None
    # Time-varying auction state, unlike everything else on this model - the
    # bid keeps changing while the listing is live, so it can't be
    # recovered later by re-mapping raw_json the way a static field could.
    # For an AUCTION-only listing, price_cents IS the current bid; for a
    # listing offering both AUCTION and FIXED_PRICE, price_cents is the BIN
    # and this is the separate bid price. Reconciling the two is a V0.8
    # scoring decision, not done here.
    current_bid_cents: int | None = None
    bid_count: int | None = None
    # None means UNKNOWN (local pickup, unresolved calculated freight), never
    # free. About 1 in 10 items in a live pull carry no shipping cost at all;
    # defaulting that to 0 would understate total_cents, make those listings
    # look like deals, and drag bucket medians down so ordinary listings
    # start scoring as deals too. Excluded from baselines at V0.8, same
    # treatment as an unparseable Spec (design.md §5.2) - still alertable on
    # price alone, just not a vote on what "normal" costs.
    shipping_cents: int | None = None

    @property
    def total_cents(self) -> int | None:
        if self.shipping_cents is None:
            return None
        return self.price_cents + self.shipping_cents


def _to_cents(value: str) -> int:
    return round(Decimal(value) * 100)


def _to_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_cents(value: object) -> int | None:
    if not isinstance(value, str):
        return None
    try:
        return _to_cents(value)
    except InvalidOperation:
        return None


def _get(node: object, *keys: str) -> object | None:
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _shipping_cents(raw: dict) -> int | None:
    options = raw.get("shippingOptions") or []
    if not options:
        return None
    return _optional_cents(_get(options[0], "shippingCost", "value"))


def parse_variation_id(item_id: str) -> str | None:
    """eBay-specific item_id parsing (V0.8d, design.md's dated entry): a
    row that is one variation of a multi-variation listing has an item_id
    of the form v1|<listing>|<variation>. Any other shape - a plain
    v1|<listing>, a malformed string, empty - returns None. Never raises:
    a malformed item_id must not abort a sweep.

    Kept here rather than in engine/baselines.py, which excludes these
    from the survival baseline: this module is already eBay-specific
    (map_item_summary), and provider ID-format knowledge belongs with the
    provider, not with the generic baseline-derivation layer.

    The exact same rule is re-implemented in SQL in storage/sqlite.py's
    migration 5 backfill - see that migration's comment for why it can't
    just call this function, and the two must be kept in lockstep by hand.
    """
    parts = item_id.split("|")
    if len(parts) != 3:
        return None
    variation = parts[2]
    if variation in ("", "0"):
        return None
    return variation


def map_item_summary(raw: dict, seen_at: datetime) -> Listing:
    """Map one raw itemSummary dict to a Listing.

    Forgiving everywhere except item_id/title/price: at V0.6 an exception
    here means a dropped listing, and dropped listings are lost history
    that can't be re-fetched. A missing nested key anywhere else just
    yields None for that field.
    """
    try:
        item_id = raw["itemId"]
        title = raw["title"]
        price_cents = _to_cents(raw["price"]["value"])
    except (KeyError, TypeError, InvalidOperation) as exc:
        raise ListingMappingError(
            f"itemSummary missing or malformed a required field: {exc}"
        ) from exc

    return Listing(
        item_id=item_id,
        title=title,
        price_cents=price_cents,
        seen_at=seen_at,
        variation_id=parse_variation_id(item_id),
        subtitle=raw.get("subtitle"),
        condition_id=_to_int(raw.get("conditionId")),
        seller=_get(raw, "seller", "username"),
        seller_feedback_pct=_to_float(_get(raw, "seller", "feedbackPercentage")),
        seller_feedback_score=_to_int(_get(raw, "seller", "feedbackScore")),
        buying_options=raw.get("buyingOptions") or [],
        item_web_url=raw.get("itemWebUrl"),
        item_location_country=_get(raw, "itemLocation", "country"),
        end_date=_parse_datetime(raw.get("itemEndDate")),
        shipping_cents=_shipping_cents(raw),
        current_bid_cents=_optional_cents(_get(raw, "currentBidPrice", "value")),
        bid_count=_to_int(raw.get("bidCount")),
    )


def normalize_input_fields(title: str, raw: dict) -> dict:
    """The dict shape dealwatch.normalize.engine.normalize() expects, built
    directly from a raw itemSummary dict that failed map_item_summary() -
    so a mapping failure (missing/malformed price is the known real case;
    design.md "persist raw before mapping") can still get a spec. Shared by
    dealwatch.engine.collector (inline, on every sighting) and
    scripts/backfill_normalize.py (over history) - one implementation, so
    the two can't drift into extracting title/subtitle/condition_id
    differently just because they're edited at different times.

    condition_id/subtitle read the same way map_item_summary does; a
    missing price says nothing about whether the profile's reject/require/
    extract rules have what they need. subtitle is always None in practice
    (Browse returns none, design.md §5.1) but the key stays present rather
    than absent, matching what a mapped Listing's model_dump() would give
    on the success path - a rule naming `subtitle` should see "never
    matches", not a dict lookup miss.

    Callers are responsible for confirming `title` is present before
    calling this - there is no fallback for a missing title, the same way
    map_item_summary itself has none.
    """
    return {
        "title": title,
        "subtitle": raw.get("subtitle"),
        "condition_id": _to_int(raw.get("conditionId")),
    }
