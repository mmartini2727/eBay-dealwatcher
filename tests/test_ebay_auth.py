"""Tests for TokenManager (dealwatch.providers.ebay_auth).

No live eBay credentials are ever needed: every test drives an
httpx.AsyncClient through httpx.MockTransport, so the network layer is fully
mocked. Tests are plain sync functions that drive their async body via
asyncio.run() - this avoids pulling in pytest-asyncio for four tests.
"""

import asyncio

import httpx
import pytest

from dealwatch.config import Settings
from dealwatch.providers.ebay_auth import TokenManager


def make_settings() -> Settings:
    # Explicit constructor args always win over anything pydantic-settings
    # would otherwise load from a real .env file, so this is safe to run
    # from a checkout that has real credentials configured.
    return Settings(ebay_client_id="test-id", ebay_client_secret="test-secret")


class FakeClock:
    """A controllable stand-in for time.monotonic().

    Real monotonic time can't be rewound in a test, so TokenManager accepts
    an injectable clock - this lets tests jump straight to "200 seconds
    before expiry" instead of actually sleeping for 7000 seconds.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_cache_hit_returns_without_network_call():
    asyncio.run(_cache_hit_returns_without_network_call())


async def _cache_hit_returns_without_network_call():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 7200})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clock = FakeClock()
    tm = TokenManager(make_settings(), client=client, clock=clock)

    token1 = await tm.get_token()
    assert token1 == "tok-1"
    assert calls == 1

    # Well inside the 7200s lifetime and outside the 5-minute margin - a
    # cache hit, no second mint.
    clock.advance(10)
    token2 = await tm.get_token()
    assert token2 == "tok-1"
    assert calls == 1

    await client.aclose()


def test_refresh_fires_inside_margin():
    asyncio.run(_refresh_fires_inside_margin())


async def _refresh_fires_inside_margin():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200, json={"access_token": f"tok-{calls}", "expires_in": 7200}
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    clock = FakeClock()
    tm = TokenManager(make_settings(), client=client, clock=clock)

    token1 = await tm.get_token()
    assert token1 == "tok-1"
    assert calls == 1

    # Jump to 200 seconds before expiry - inside the 5-minute (300s) margin,
    # so this must mint a new token rather than serving the stale one.
    clock.advance(7200 - 200)
    token2 = await tm.get_token()
    assert token2 == "tok-2"
    assert calls == 2

    await client.aclose()


def test_concurrent_callers_mint_exactly_once():
    asyncio.run(_concurrent_callers_mint_exactly_once())


async def _concurrent_callers_mint_exactly_once():
    calls = 0
    mint_started = asyncio.Event()
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        # Hold the "in-flight" mint open until both callers have arrived,
        # so a real race would show up as calls == 2 instead of 1.
        mint_started.set()
        await release.wait()
        return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 7200})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tm = TokenManager(make_settings(), client=client, clock=FakeClock())

    task1 = asyncio.create_task(tm.get_token())
    task2 = asyncio.create_task(tm.get_token())

    await mint_started.wait()
    release.set()

    token1, token2 = await asyncio.gather(task1, task2)
    assert token1 == token2 == "tok-1"
    assert calls == 1

    await client.aclose()


def test_401_triggers_one_retry_then_propagates():
    asyncio.run(_401_triggers_one_retry_then_propagates())


async def _401_triggers_one_retry_then_propagates():
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, json={"access_token": "tok-1", "expires_in": 7200}
            )
        # Simulate eBay rejecting the credentials on every mint after the
        # first - e.g. a revoked keyset.
        return httpx.Response(401, json={"error": "invalid_client"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    tm = TokenManager(make_settings(), client=client, clock=FakeClock())

    token1 = await tm.get_token()
    assert token1 == "tok-1"
    assert calls == 1

    # Caller got a 401 using tok-1 downstream and forces exactly one retry.
    with pytest.raises(httpx.HTTPStatusError):
        await tm.get_token(force_refresh=True)
    assert calls == 2

    # A failed mint must not corrupt the cache: the last good token is still
    # served (without a network call) until something explicitly forces a
    # refresh again.
    token_after_failure = await tm.get_token()
    assert token_after_failure == "tok-1"
    assert calls == 2

    # And a second forced retry is a caller decision, not something
    # TokenManager loops on internally - it tries once and propagates again.
    with pytest.raises(httpx.HTTPStatusError):
        await tm.get_token(force_refresh=True)
    assert calls == 3

    await client.aclose()
