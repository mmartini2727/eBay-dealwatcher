"""eBay Browse API client - item_summary/search only.

Deliberately thin. Normalization, the collector loop, and polling strategy
(sort=newlyListed, sweep intervals, etc. from design.md §7) are later
milestones - this exists to prove the OAuth (V0.2) and budget (this
milestone) plumbing works end-to-end, and to hand back raw eBay dicts for
V0.4+ to normalize. No Listing model here on purpose.
"""

import asyncio
from typing import Any

import httpx

from dealwatch.config import Settings
from dealwatch.normalize.schema import Profile
from dealwatch.providers.base import MarketplaceProvider
from dealwatch.providers.ebay_auth import TokenManager
from dealwatch.providers.ratelimit import BudgetExhausted, DailyBudget

SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

# Filters whose value is a two-element [min, max] list and render as an
# inclusive eBay range (`key:[min..max]`); everything else with a list
# value renders as a set (`key:{a|b|c}`). eBay's grammar has no way to tell
# these apart from the value's shape alone (conditionIds is also a list of
# numbers), so the key has to say which one it is.
_RANGE_FILTERS = {"price", "itemStartDate"}


class RateLimited(Exception):
    """Raised on a 429 from eBay. Never retried - retrying while already
    over a rate limit just makes it worse."""


def build_filter_string(filters: dict[str, Any]) -> str:
    parts = []
    for key, value in filters.items():
        if isinstance(value, list):
            if key in _RANGE_FILTERS:
                lo, hi = value
                parts.append(f"{key}:[{lo}..{hi}]")
            else:
                joined = "|".join(str(v) for v in value)
                parts.append(f"{key}:{{{joined}}}")
        else:
            parts.append(f"{key}:{value}")
    return ",".join(parts)


class EbayBrowseProvider(MarketplaceProvider):
    def __init__(
        self,
        settings: Settings,
        token_manager: TokenManager,
        budget: DailyBudget,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._tokens = token_manager
        self._budget = budget
        self._client = client if client is not None else httpx.AsyncClient()
        self._owns_client = client is None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def search(
        self,
        profile: Profile,
        query: str,
        *,
        limit: int = 50,
        max_pages: int = 1,
    ) -> list[dict]:
        filter_string = build_filter_string(profile.search.filters)
        results: list[dict] = []

        for page in range(max_pages):
            params: dict[str, str] = {
                "q": query,
                "limit": str(limit),
                "offset": str(page * limit),
            }
            if filter_string:
                params["filter"] = filter_string
            if profile.search.category_ids:
                params["category_ids"] = ",".join(profile.search.category_ids)

            try:
                payload = await self._request(params)
            except BudgetExhausted:
                # Earlier pages in this call already cost real budget and
                # returned real listings - those are collected history that
                # can't be re-fetched for free later, so don't throw them
                # away because a later page in the same call ran out of
                # room. Only propagate if this is the first page and there's
                # nothing to salvage.
                if results:
                    return results
                raise

            items = payload.get("itemSummaries", [])
            results.extend(items)

            if len(items) < limit:
                break  # short page - nothing more to paginate

        return results

    async def _request(self, params: dict[str, str]) -> dict:
        response, token = await self._attempt(params)

        if response.status_code == 401:
            # V0.2 deliberately left this retry to the caller: TokenManager
            # only mints, it doesn't know what "the request that used the
            # token" was - so we tell it, by name, which token just failed.
            # One retry, then propagate.
            response, _token = await self._attempt(params, stale_token=token)

        if response.status_code == 429:
            raise RateLimited(response.text)

        response.raise_for_status()
        return response.json()

    async def _attempt(
        self, params: dict[str, str], *, stale_token: str | None = None
    ) -> tuple[httpx.Response, str]:
        # Reserve before any network activity for this attempt - including
        # before minting a token - so an exhausted budget makes zero HTTP
        # calls, not just zero search calls. Reserved again on the 401
        # retry: eBay counts that as a second real request against Browse.
        if not await asyncio.to_thread(self._budget.reserve):
            raise BudgetExhausted(self._budget.status())

        token = await self._tokens.get_token(stale_token=stale_token)
        headers = {
            "Authorization": f"Bearer {token}",
            "X-EBAY-C-MARKETPLACE-ID": self._settings.ebay_marketplace_id,
            "X-EBAY-C-ENDUSERCTX": self._contextual_location(),
        }
        response = await self._client.get(SEARCH_URL, params=params, headers=headers)
        return response, token

    def _contextual_location(self) -> str:
        value = f"contextualLocation=country={self._settings.ebay_location_country}"
        if self._settings.ebay_location_zip:
            value += f",zip={self._settings.ebay_location_zip}"
        return value
