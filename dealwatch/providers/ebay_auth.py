"""eBay OAuth application access tokens (client credentials grant).

Application tokens authenticate DealWatch to eBay, not a user. eBay does not
issue a refresh token for this grant type - there is nothing to refresh, only
a new token to mint. That is fine: minting is cheap and the token is never
persisted, so a container restart just mints a fresh one on first use.
"""

import asyncio
import base64
import time
from collections.abc import Callable

import httpx

from dealwatch.config import Settings

TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SCOPE = "https://api.ebay.com/oauth/api_scope"

# Refresh once fewer than this many seconds remain, rather than waiting for
# the token to actually expire. A request that starts just before expiry can
# still be mid-flight when eBay rejects it.
REFRESH_MARGIN_SECONDS = 5 * 60


class TokenManager:
    """Mints and caches an eBay application access token in memory.

    The collector and the MCP server both hold a reference to the same
    TokenManager, so refreshes are guarded by an asyncio.Lock: without it,
    two callers racing past an expired token would each mint their own,
    burning eBay's token-request budget for no benefit.
    """

    def __init__(
        self,
        settings: Settings,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        # If no client is given, we own the one we create and must close it
        # ourselves; a caller-supplied client (e.g. a shared client, or a
        # test's MockTransport-backed one) is theirs to close.
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None
        # time.monotonic() by default: wall-clock time can jump (NTP, DST);
        # a monotonic clock can't, so expiry math never goes backwards.
        self._clock = clock

        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at: float | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def get_token(self, *, force_refresh: bool = False) -> str:
        """Return a cached token, or mint one if it's stale or forced.

        force_refresh exists so a caller that just got a 401 from eBay (the
        cached token looked valid locally but eBay disagrees) can ask for a
        guaranteed-fresh token and retry the request exactly once. If the
        retry also fails, that's a real auth problem - the caller should let
        the error propagate rather than looping.
        """
        if not force_refresh and self._is_fresh():
            assert self._token is not None
            return self._token

        async with self._lock:
            # Re-check after acquiring the lock: another caller may have
            # already refreshed while we were waiting on it. Skipped for
            # force_refresh, since the whole point there is "mint me a
            # token eBay hasn't already rejected."
            if not force_refresh and self._is_fresh():
                assert self._token is not None
                return self._token
            await self._mint()
            assert self._token is not None
            return self._token

    def _is_fresh(self) -> bool:
        if self._token is None or self._expires_at is None:
            return False
        return self._clock() < self._expires_at - REFRESH_MARGIN_SECONDS

    async def _mint(self) -> None:
        if not self._settings.ebay_client_id or not self._settings.ebay_client_secret:
            raise RuntimeError(
                "EBAY_CLIENT_ID / EBAY_CLIENT_SECRET are not configured"
            )

        credentials = f"{self._settings.ebay_client_id}:{self._settings.ebay_client_secret}"
        basic = base64.b64encode(credentials.encode("utf-8")).decode("ascii")

        response = await self._client.post(
            TOKEN_URL,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "client_credentials",
                "scope": SCOPE,
            },
        )
        # Deliberately not caught here: a failed mint should propagate to
        # the caller rather than being retried in a loop or silently cached
        # as a "success". See get_token()'s force_refresh docstring.
        response.raise_for_status()

        payload = response.json()
        self._token = payload["access_token"]
        self._expires_at = self._clock() + payload["expires_in"]
