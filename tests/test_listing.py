"""Tests for dealwatch.normalize.listing (map_item_summary / Listing).

Titles come from tests/fixtures/titles.txt - real listing titles pulled
from live eBay results, not invented ones, so title content itself isn't
special-cased anywhere in this file.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from dealwatch.normalize.listing import ListingMappingError, map_item_summary

TITLES = (
    Path(__file__).parent / "fixtures" / "titles.txt"
).read_text().strip().splitlines()
TITLES = [line.strip().lstrip("- ").strip() for line in TITLES]

SEEN_AT = datetime(2026, 8, 29, 12, 0, tzinfo=timezone.utc)


def full_item_summary(**overrides) -> dict:
    # Shape matches eBay Browse API's item_summary/search response schema -
    # conditionId is a string in the real API, price/shippingCost values are
    # decimal strings, dates are ISO-8601 with a Z suffix.
    base = {
        "itemId": "v1|110599695364|0",
        "title": TITLES[0],
        "subtitle": "Ships fast from a top-rated seller",
        "price": {"value": "349.99", "currency": "USD"},
        "condition": "Used",
        "conditionId": "3000",
        "seller": {
            "username": "refurb_liquidators",
            "feedbackPercentage": "99.5",
            "feedbackScore": 40213,
        },
        "buyingOptions": ["FIXED_PRICE"],
        "itemWebUrl": "https://www.ebay.com/itm/110599695364",
        "itemLocation": {"country": "US", "postalCode": "9***"},
        "itemEndDate": "2026-09-15T18:30:00.000Z",
        "shippingOptions": [
            {
                "shippingCostType": "FIXED",
                "shippingCost": {"value": "12.50", "currency": "USD"},
            }
        ],
    }
    base.update(overrides)
    return base


def test_normal_complete_item_maps_every_field():
    listing = map_item_summary(full_item_summary(), seen_at=SEEN_AT)

    assert listing.item_id == "v1|110599695364|0"
    assert listing.title == TITLES[0]
    assert listing.price_cents == 34999
    assert listing.seen_at == SEEN_AT
    assert listing.subtitle == "Ships fast from a top-rated seller"
    assert listing.condition_id == 3000
    assert listing.seller == "refurb_liquidators"
    assert listing.seller_feedback_pct == 99.5
    assert listing.seller_feedback_score == 40213
    assert listing.buying_options == ["FIXED_PRICE"]
    assert listing.item_web_url == "https://www.ebay.com/itm/110599695364"
    assert listing.item_location_country == "US"
    assert listing.shipping_cents == 1250
    assert listing.total_cents == 34999 + 1250


def test_price_string_converts_to_integer_cents():
    listing = map_item_summary(
        full_item_summary(price={"value": "349.99", "currency": "USD"}),
        seen_at=SEEN_AT,
    )
    # Not a float (349.99) and not a truncated 349 - "349.99" means 34999
    # cents exactly.
    assert listing.price_cents == 34999
    assert isinstance(listing.price_cents, int)


def test_free_shipping_is_zero_not_unknown():
    raw = full_item_summary(
        shippingOptions=[
            {"shippingCostType": "FIXED", "shippingCost": {"value": "0.00", "currency": "USD"}}
        ]
    )
    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.shipping_cents == 0
    assert listing.total_cents == listing.price_cents


def test_missing_shipping_options_is_unknown_not_free():
    # The load-bearing test: shipping_cents must be None here, not 0.
    # Defaulting to 0 would make an unresolved-shipping listing look
    # artificially cheap and would drag bucket medians down at V0.8.
    raw = full_item_summary()
    del raw["shippingOptions"]

    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.shipping_cents is None
    assert listing.total_cents is None


def test_empty_shipping_options_list_is_unknown_not_free():
    raw = full_item_summary(shippingOptions=[])

    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.shipping_cents is None
    assert listing.total_cents is None


def test_missing_optional_nested_fields_yield_none_not_exception():
    raw = full_item_summary()
    del raw["seller"]
    del raw["subtitle"]
    del raw["itemEndDate"]

    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.seller is None
    assert listing.subtitle is None
    assert listing.end_date is None


def test_fixed_price_item_has_no_bid_fields():
    # The base fixture is FIXED_PRICE and carries no currentBidPrice/
    # bidCount at all - that's normal, not a mapping failure.
    listing = map_item_summary(full_item_summary(), seen_at=SEEN_AT)

    assert listing.current_bid_cents is None
    assert listing.bid_count is None


def test_auction_item_populates_bid_fields():
    raw = full_item_summary(
        buyingOptions=["AUCTION"],
        currentBidPrice={"value": "125.50", "currency": "USD"},
        bidCount=7,
    )

    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.current_bid_cents == 12550
    assert listing.bid_count == 7


def test_seller_feedback_percentage_string_converts_to_float():
    raw = full_item_summary(
        seller={"username": "refurb_liquidators", "feedbackPercentage": "99.3"}
    )

    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.seller_feedback_pct == 99.3
    assert isinstance(listing.seller_feedback_pct, float)


def test_missing_seller_block_leaves_both_feedback_fields_none():
    raw = full_item_summary()
    del raw["seller"]

    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.seller_feedback_pct is None
    assert listing.seller_feedback_score is None


def test_non_numeric_feedback_percentage_is_none_not_zero():
    raw = full_item_summary(
        seller={"username": "refurb_liquidators", "feedbackPercentage": "N/A"}
    )

    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.seller_feedback_pct is None


def test_missing_item_id_raises_listing_mapping_error():
    raw = full_item_summary()
    del raw["itemId"]

    with pytest.raises(ListingMappingError):
        map_item_summary(raw, seen_at=SEEN_AT)


def test_missing_price_raises_listing_mapping_error():
    raw = full_item_summary()
    del raw["price"]

    with pytest.raises(ListingMappingError):
        map_item_summary(raw, seen_at=SEEN_AT)


def test_end_date_parses_to_timezone_aware_utc():
    listing = map_item_summary(full_item_summary(), seen_at=SEEN_AT)

    assert listing.end_date is not None
    assert listing.end_date.tzinfo is not None
    assert listing.end_date.utcoffset() == timedelta(0)


def test_missing_buying_options_is_empty_list_not_none():
    raw = full_item_summary()
    del raw["buyingOptions"]

    listing = map_item_summary(raw, seen_at=SEEN_AT)

    assert listing.buying_options == []


@pytest.mark.parametrize("title", TITLES)
def test_every_fixture_title_maps_through_verbatim(title):
    # Title parsing (Spec extraction) is V0.7 - at V0.4 the mapper must not
    # alter, reject, or otherwise interpret the title string at all.
    listing = map_item_summary(full_item_summary(title=title), seen_at=SEEN_AT)
    assert listing.title == title
