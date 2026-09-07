#!/usr/bin/env python3
"""V0.8e decision gate: does eBay actually honor `sort=newlyListed`?

    python scripts/probe_sort.py --profile profiles/thinkpad-t14.yaml

Makes exactly two live Browse API calls - the poll's exact query as it is
sent today, then the same query with sort=newlyListed added - and prints a
comparison. Reads live credentials from the environment the same way the
app does (Settings()); run this on the LXC via docker cp, not on a checkout
with no .env.

Reaches into EbayBrowseProvider._request() directly to get at the response
envelope's `total` field, which search() discards. That is a throwaway-probe
shortcut, not a precedent - search() itself is not changed here.

This does not touch any production code path. It exists to answer one
question before V0.8e writes a single line of wiring: does the parameter do
anything, and if so, does it do what design.md §5's discovery-latency
argument assumes? See design.md for the three possible outcomes and what
each one means for the milestone.
"""

import argparse
import asyncio
import statistics
from datetime import datetime, timezone

import httpx

from dealwatch.config import get_settings
from dealwatch.engine.collector import FAST_POLL_PAGE_LIMIT, load_profile
from dealwatch.normalize.listing import _parse_datetime
from dealwatch.providers.ebay import EbayBrowseProvider, build_filter_string
from dealwatch.providers.ebay_auth import TokenManager
from dealwatch.providers.ratelimit import DailyBudget


def _creation_date_stats(items: list[dict]) -> str:
    epochs = []
    for item in items:
        dt = _parse_datetime(item.get("itemCreationDate"))
        if dt is not None:
            epochs.append(dt.timestamp())
    if not epochs:
        return f"n=0/{len(items)} (no parseable itemCreationDate values)"

    def fmt(epoch: float) -> str:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()

    return (
        f"n={len(epochs)}/{len(items)}  "
        f"min={fmt(min(epochs))}  "
        f"median={fmt(statistics.median(epochs))}  "
        f"max={fmt(max(epochs))}"
    )


def _report(label: str, payload: dict) -> list[str]:
    items = payload.get("itemSummaries", [])
    item_ids = [item.get("itemId") for item in items]
    print(f"--- {label} ---")
    print(f"count returned : {len(items)}")
    print(f"total (envelope): {payload.get('total')}")
    print(f"first 10 item_ids: {item_ids[:10]}")
    print(f"itemCreationDate : {_creation_date_stats(items)}")
    return item_ids


async def main(profile_path: str) -> None:
    settings = get_settings()
    profile = load_profile(profile_path)
    query = profile.search.queries[0]

    token_manager = TokenManager(settings)
    budget = DailyBudget(settings)
    provider = EbayBrowseProvider(settings, token_manager, budget)

    filter_string = build_filter_string(profile.search.filters)
    base_params: dict[str, str] = {
        "q": query,
        "limit": str(FAST_POLL_PAGE_LIMIT),
        "offset": "0",
    }
    if filter_string:
        base_params["filter"] = filter_string
    if profile.search.category_ids:
        base_params["category_ids"] = ",".join(profile.search.category_ids)

    try:
        print(f"query={query!r} limit={FAST_POLL_PAGE_LIMIT}")
        print()

        payload_unsorted = await provider._request(dict(base_params))
        ids_unsorted = _report("unsorted (today's actual behavior)", payload_unsorted)
        print()

        sorted_params = dict(base_params)
        sorted_params["sort"] = "newlyListed"

        try:
            payload_sorted = await provider._request(sorted_params)
        except httpx.HTTPStatusError as exc:
            print("--- sort=newlyListed ---")
            print(f"HTTP {exc.response.status_code} - eBay rejected the parameter")
            print(exc.response.text)
            return

        ids_sorted = _report("sort=newlyListed", payload_sorted)
        print()

        symmetric_diff = set(ids_unsorted) ^ set(ids_sorted)
        print(f"symmetric difference of item_id sets: {len(symmetric_diff)}")
    finally:
        await provider.aclose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True, help="path to a profiles/*.yaml file")
    args = parser.parse_args()
    asyncio.run(main(args.profile))
