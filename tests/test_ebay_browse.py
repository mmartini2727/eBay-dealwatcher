"""Tests for EbayBrowseProvider (dealwatch.providers.ebay).

Both the token endpoint and the search endpoint are mocked via separate
httpx.MockTransport-backed clients (mirroring how TokenManager and
EbayBrowseProvider hold independent httpx.AsyncClients in production), so
no test needs live eBay credentials.
"""

import asyncio

import httpx
import pytest

from dealwatch.config import Settings
from dealwatch.normalize.schema import Profile, SearchConfig
from dealwatch.providers.ebay import EbayBrowseProvider, RateLimited, build_filter_string
from dealwatch.providers.ebay_auth import TokenManager
from dealwatch.providers.ratelimit import BudgetExhausted, DailyBudget


def make_settings(tmp_path, **overrides):
    defaults = dict(
        ebay_client_id="test-id",
        ebay_client_secret="test-secret",
        db_path=str(tmp_path / "dealwatch.db"),
        daily_call_limit=1000,
        daily_reserve_calls=0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def make_profile(filters=None):
    return Profile(
        id="thinkpad-t14",
        name="Lenovo ThinkPad T14",
        search=SearchConfig(
            queries=["Lenovo ThinkPad T14"],
            category_ids=["177"],
            filters=filters if filters is not None else {"price": [80, 1200]},
        ),
    )


def counting_token_handler(calls: list, *, token="tok"):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200, json={"access_token": f"{token}-{len(calls)}", "expires_in": 7200}
        )

    return handler


def run(coro):
    return asyncio.run(coro)


def test_build_filter_string_renders_ranges_and_sets():
    filters = {
        "conditionIds": [2000, 2500, 3000],
        "buyingOptions": ["FIXED_PRICE", "AUCTION", "BEST_OFFER"],
        "price": [80, 1200],
        "priceCurrency": "USD",
        "itemLocationCountry": "US",
    }
    assert build_filter_string(filters) == (
        "conditionIds:{2000|2500|3000},"
        "buyingOptions:{FIXED_PRICE|AUCTION|BEST_OFFER},"
        "price:[80..1200],"
        "priceCurrency:USD,"
        "itemLocationCountry:US"
    )


def test_401_triggers_exactly_one_refresh_and_one_retry(tmp_path):
    run(_401_triggers_exactly_one_refresh_and_one_retry(tmp_path))


async def _401_triggers_exactly_one_refresh_and_one_retry(tmp_path):
    token_calls: list = []
    token_client = httpx.AsyncClient(
        transport=httpx.MockTransport(counting_token_handler(token_calls))
    )
    tm = TokenManager(make_settings(tmp_path), client=token_client)

    search_calls: list = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_calls.append(request)
        if len(search_calls) == 1:
            return httpx.Response(401, json={"error": "expired"})
        return httpx.Response(200, json={"itemSummaries": [{"itemId": "1"}]})

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(search_handler))
    budget = DailyBudget(make_settings(tmp_path))
    provider = EbayBrowseProvider(
        make_settings(tmp_path), tm, budget, client=search_client
    )

    items = await provider.search(make_profile(), "Lenovo ThinkPad T14")

    assert items == [{"itemId": "1"}]
    assert len(search_calls) == 2
    assert len(token_calls) == 2  # initial mint + refresh on the 401

    # A mint happening isn't proof the new token reached the wire - confirm
    # the retry actually carried a different Authorization header, not the
    # same rejected one.
    assert (
        search_calls[0].headers["Authorization"]
        != search_calls[1].headers["Authorization"]
    )

    # The 401 retry is a second real request against Browse and re-reserves
    # budget accordingly (see _attempt()'s comment on this) - documented
    # behavior that had no test before.
    assert budget.status()["used"] == 2

    await token_client.aclose()
    await search_client.aclose()


def test_429_raises_without_retrying(tmp_path):
    run(_429_raises_without_retrying(tmp_path))


async def _429_raises_without_retrying(tmp_path):
    token_calls: list = []
    token_client = httpx.AsyncClient(
        transport=httpx.MockTransport(counting_token_handler(token_calls))
    )
    tm = TokenManager(make_settings(tmp_path), client=token_client)

    search_calls: list = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_calls.append(request)
        return httpx.Response(429, text="rate limited")

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(search_handler))
    budget = DailyBudget(make_settings(tmp_path))
    provider = EbayBrowseProvider(
        make_settings(tmp_path), tm, budget, client=search_client
    )

    with pytest.raises(RateLimited):
        await provider.search(make_profile(), "Lenovo ThinkPad T14")

    # Exactly one attempt - a 429 is not a 401, so no forced-refresh retry
    # path is taken, and nothing loops.
    assert len(search_calls) == 1
    assert len(token_calls) == 1

    await token_client.aclose()
    await search_client.aclose()


def test_budget_exhausted_makes_no_http_call(tmp_path):
    run(_budget_exhausted_makes_no_http_call(tmp_path))


async def _budget_exhausted_makes_no_http_call(tmp_path):
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected HTTP call: {request.url}")

    token_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    tm = TokenManager(make_settings(tmp_path), client=token_client)

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(fail))
    # ceiling = daily_call_limit - daily_reserve_calls = 0: every reserve()
    # is refused immediately.
    exhausted_settings = make_settings(tmp_path, daily_call_limit=10, daily_reserve_calls=10)
    budget = DailyBudget(exhausted_settings)
    provider = EbayBrowseProvider(exhausted_settings, tm, budget, client=search_client)

    with pytest.raises(BudgetExhausted):
        await provider.search(make_profile(), "Lenovo ThinkPad T14")

    await token_client.aclose()
    await search_client.aclose()


def test_pagination_fetches_until_a_short_page(tmp_path):
    run(_pagination_fetches_until_a_short_page(tmp_path))


async def _pagination_fetches_until_a_short_page(tmp_path):
    token_client = httpx.AsyncClient(
        transport=httpx.MockTransport(counting_token_handler([]))
    )
    tm = TokenManager(make_settings(tmp_path), client=token_client)

    search_calls: list = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_calls.append(request)
        if len(search_calls) == 1:
            # A full page (limit=2) - there might be more, keep paginating.
            return httpx.Response(
                200,
                json={"itemSummaries": [{"itemId": "1"}, {"itemId": "2"}]},
            )
        # A short page - this is the last one.
        return httpx.Response(200, json={"itemSummaries": [{"itemId": "3"}]})

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(search_handler))
    settings = make_settings(tmp_path)
    budget = DailyBudget(settings)
    provider = EbayBrowseProvider(settings, tm, budget, client=search_client)

    items = await provider.search(
        make_profile(), "Lenovo ThinkPad T14", limit=2, max_pages=5
    )

    assert items == [{"itemId": "1"}, {"itemId": "2"}, {"itemId": "3"}]
    assert len(search_calls) == 2
    assert budget.status()["used"] == 2

    await token_client.aclose()
    await search_client.aclose()


def test_budget_exhausted_mid_pagination_returns_partial_results(tmp_path):
    run(_budget_exhausted_mid_pagination_returns_partial_results(tmp_path))


async def _budget_exhausted_mid_pagination_returns_partial_results(tmp_path):
    token_client = httpx.AsyncClient(
        transport=httpx.MockTransport(counting_token_handler([]))
    )
    tm = TokenManager(make_settings(tmp_path), client=token_client)

    search_calls: list = []

    def search_handler(request: httpx.Request) -> httpx.Response:
        search_calls.append(request)
        # Always a full page, so search() always wants a second page.
        return httpx.Response(
            200,
            json={"itemSummaries": [{"itemId": "1"}, {"itemId": "2"}]},
        )

    search_client = httpx.AsyncClient(transport=httpx.MockTransport(search_handler))
    # ceiling = 1: page one's reserve succeeds and spends the whole budget;
    # page two's reserve is refused before any second HTTP call is made.
    settings = make_settings(tmp_path, daily_call_limit=1, daily_reserve_calls=0)
    budget = DailyBudget(settings)
    provider = EbayBrowseProvider(settings, tm, budget, client=search_client)

    items = await provider.search(
        make_profile(), "Lenovo ThinkPad T14", limit=2, max_pages=5
    )

    # Page one's listings are real, already-paid-for history - returned
    # rather than discarded just because page two couldn't be afforded.
    assert items == [{"itemId": "1"}, {"itemId": "2"}]
    assert len(search_calls) == 1

    await token_client.aclose()
    await search_client.aclose()
