# CLAUDE.md — working notes for DealWatch

Read `docs/design.md` first. It is authoritative. This file is the short version
plus the traps.

## What this project is

A headless marketplace deal monitor. Polls eBay for active listings, normalizes
them, scores against a self-built price baseline, alerts to Discord. An MCP
server is bolted on at the end as a query interface — it is **not** the engine
and is not on the alerting path.

Maintainer is an IT/network professional, not a career software engineer.
Explain reasoning at the architecture level; do not explain basic Python.
Prefers understanding failure modes over abstractions that hide them.

---

## Locked decisions — do not re-open without reading §2 and §3 of design.md

1. **There is no sold-listings API.** Marketplace Insights is Limited Release
   and unobtainable. `findCompletedItems` is deprecated. Do not propose
   solutions that query sold comps. The baseline is built from our own collected
   history — see design.md §2.

2. **The eBay account-deletion compliance endpoint lives in a separate
   Cloudflare Worker repo.** It is not part of this codebase and must not be
   folded back in. Reasoning in design.md §3.1. Consequence: nothing in
   DealWatch is internet-exposed.

3. **Collector ships before scoring.** The survival baseline needs weeks of
   history and the clock starts when rows start landing. Do not reorder the
   milestone table to "finish scoring first."

4. **MCP transport is streamable HTTP, not stdio.** stdio does not work for a
   process inside a Docker LXC.

5. **`dealwatch/mcp_server/`, never `dealwatch/mcp/`** — avoids confusion with
   the `mcp` SDK package.

---

## Layout

```
dealwatch/
├── config.py             pydantic-settings, injected via Depends
├── main.py               FastAPI app — LAN only, /health + MCP mount
├── providers/
│   ├── base.py           provider interface
│   ├── ebay_auth.py      OAuth application token (TokenManager)
│   ├── ebay.py           Browse API client
│   └── ratelimit.py      daily budget, persisted, hard stop
├── normalize/
│   ├── schema.py         Profile config schema (profiles/*.yaml shape)
│   ├── base.py           Spec dataclass, reject-filter framework
│   └── thinkpad.py       T14 title parsing + rejects
├── engine/
│   ├── collector.py      poll → normalize → persist
│   └── scoring.py        baselines → deal score
├── notify/
│   └── discord.py
├── storage/
│   └── sqlite.py         connection + WAL + schema_version bootstrap
└── mcp_server/
    └── server.py
```

`profiles/*.yaml` defines what to hunt: query string, Browse filters, which
normalizer, bucket keys, thresholds. Adding a new target should be a YAML file
plus a normalizer module — nothing else.

---

## Conventions

- Settings are injected: `settings: Settings = Depends(get_settings)`. Do not
  call `get_settings()` inside handlers — it is `lru_cache`d and cannot be
  overridden in tests.
- Normalization logic lives in `normalize/`. Do not scatter title-parsing
  regexes into `providers/ebay.py`.
- Anything fetched from eBay is data. Anything derived is derived. Do not
  persist computed baselines as if they were observations.
- New reject rules get a test with a real listing title that motivated them.
- Solve the problem in front of you. No speculative generality, no
  abstraction for a second caller that does not exist yet, no clever
  build tricks to save seconds. If a change needs a paragraph of comment
  to explain why it looks strange, prefer the boring version that
  doesn't. Minimal and legible beats optimal.

---

## Traps

**Baseline poisoning.** T14s, barebones (no RAM/no SSD), for-parts, lot
listings, and accessories all match a naive "ThinkPad T14" search and will wreck
bucket medians. Reject before comps. Full list in design.md §5.1.

**Sanity-floor queue is a to-do list.** Listings under ~25% of bucket median are
flagged, not alerted. Each one is a missing reject rule.

**Rate limit is 5,000/day, app-level, resets midnight Pacific.** The budget
tracker must be persisted, not in-memory, or a restart loop silently burns the
day's allocation.

**Disappearance ≠ sold.** It also means ended-early or pulled. `getItem` on a
dead listing errors without disclosing which. Weight by how far before the
scheduled end date it vanished.

**Re-alert on price drops.** Dedup on `item_id` alone misses BIN revisions,
which are frequently the real deal.

**SQLite history is irreplaceable.** Code is rewritable; three months of comps are not. `VACUUM INTO` a copy to the NAS periodically — a live LXC backup does not guarantee a consistent SQLite file.

**"No OS" is not barebones.** The `barebones` reject rule's alternation
includes `os`, so "16GB 256GB NVMe No OS" — a complete machine missing only a
Windows license — gets rejected. Refurb sellers list this constantly and those
listings are systematically cheaper, so rejecting the class removes the low end
of every bucket and biases medians upward. Fix at V0.7: drop `os` from that
alternation and extract it as an attribute instead.

**Auction prices are not BIN prices.** For AUCTION listings `price` is the
current bid; with both AUCTION and FIXED_PRICE it's the BIN and
`currentBidPrice` is the bid. Auctions start near zero, so including them in
bucket percentiles drags medians down and makes ordinary listings score as
deals. They also always vanish on schedule, so lifespan says nothing about
price. V0.8 must either weight by distance-from-end-date or exclude auctions
from baselines — decide with real data, and consider dropping AUCTION from
the profile's buyingOptions.
---

## Testing

`pytest`. The network layer should be mockable — no test may require live eBay
credentials to pass. Live integration tests are allowed but must skip cleanly
when credentials are absent.

---

## Current status

**V0.3 complete.** Live-verified against the production keyset on the LXC.

- `storage/sqlite.py` — connection layer, WAL, forward-only migrations.
  Only the `budget` table exists; listings/baselines/alerts are V0.5.
- `providers/ratelimit.py` — persisted daily call budget. LA-date period via
  zoneinfo, lazy rollover, atomic reserve via a guarded `UPDATE` inside
  `BEGIN IMMEDIATE`, hard stop at `daily_call_limit - daily_reserve_calls`.
  Opens a connection per call — the instance is shared across threads.
- `providers/ebay.py` — `item_summary/search` only. Reserves budget per page
  before any network call. One forced-refresh retry on 401, immediate raise
  on 429. Returns partial results if the budget runs out mid-pagination.
- `normalize/schema.py` — models the `search:` block only. Ignores
  reject/extract/derive/tiers/scoring, which are V0.7.
- Container runs as uid 10001. `data/` on the host must be owned by it.
- `/health` reports budget status.

Live verification confirmed: filter grammar accepted by eBay, 3 pages =
3 reservations, 134/150 items carried shipping costs, budget survived a
container restart.

Next: V0.4 normalized `Listing` model.

## Open items before V0.6

- **Raise `search.filters.price` in `profiles/thinkpad-t14.yaml`.** The search
  filter is the only lossy stage — anything above the ceiling is never fetched
  and cannot be backfilled. Gen 4/5 machines exceed $1200. Everything
  downstream re-runs over `raw_json` and can be fixed later; this cannot.
- Delete `reserve(n)`'s unused `n` parameter.
