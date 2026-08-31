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

Files marked (V0.x) do not exist yet — the milestone that creates them is
noted. Everything else is on disk today.
```
dealwatch/
├── config.py pydantic-settings, injected via Depends
├── main.py FastAPI app — LAN only, /health
├── providers/
│ ├── base.py provider interface
│ ├── ebay_auth.py OAuth application token (TokenManager)
│ ├── ebay.py Browse API client — item_summary/search only
│ └── ratelimit.py daily budget, persisted, hard stop
├── normalize/
│ ├── schema.py profile YAML shape (models search: only so far)
│ ├── listing.py Listing model + map_item_summary()
│ ├── engine.py (V0.7) generic profile interpreter:
│ │ reject → extract → derive → tiers → bucket_key
│ ├── functions.py (V0.7) named transforms for apply: (to_int, tb_to_gb)
│ └── explain.py (V0.7) CLI: trace one title through the pipeline
├── engine/
│ ├── collector.py (V0.6) poll → persist raw → map → persist
│ └── scoring.py (V0.8) baselines → deal score
├── notify/
│ └── discord.py (V0.9)
├── storage/
│ └── sqlite.py connection + WAL + forward-only migrations
└── mcp_server/
└── server.py (V1.0) streamable HTTP, LAN only
```

There is exactly one normalization engine and it is generic. Adding a target is a YAML file in `profiles/` — never a Python module. See locked decision #5.

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
- **Two different connection-ownership patterns exist in `storage/` on
  purpose — don't merge them.** `DailyBudget` (`providers/ratelimit.py`)
  opens and closes its own connection per call, because it's a
  `lru_cache`d singleton shared across uvicorn's threadpool and
  `asyncio.to_thread` calls — a connection can't safely be reused across
  those threads. `record_sighting`/`record_sweep`
  (`storage/sqlite.py`) instead take an already-open `conn` as their first
  argument, because V0.6's collector is a single long-lived loop calling
  them many times per poll/sweep — reopening a connection (and re-running
  the migration check) per item would be wasted work for no safety benefit.
  V0.6's collector should open one connection at startup and pass it in;
  it should not instantiate a `DailyBudget`-style per-call wrapper around
  the listings/observations write path.

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

**Persist raw before mapping.** The collector writes `raw_json` first, then
maps. A mapping failure becomes a row with raw data and null derived fields —
re-mappable once the shape is understood. Mapping first and dropping failures
loses history permanently: 5 of 150 live listings currently fail to map
(auction-only, no `price` field), and that is 3% of comps gone for a bug that
takes ten minutes to fix afterward.
---

## Testing

`pytest`. The network layer should be mockable — no test may require live eBay
credentials to pass. Live integration tests are allowed but must skip cleanly
when credentials are absent.

---

## Current status

**V0.6 complete.** Live-verified on the LXC. `engine/collector.py` runs two
independent loops against the V0.5 write path; `main.py` starts and cancels
them from the FastAPI lifespan. No normalization, scoring, or alerting.

- **Two schedules, different semantics.** The fast poll (5 min, one page)
  calls `record_sighting` only. The sweep (60 min, deep pagination) calls
  `record_sighting` per item then one `record_sweep`. Only the sweep writes
  `last_seen`. Verified live: 16 polls and 3 sweeps produced exactly 3
  distinct `last_seen` values.
- **Sweep truncation is inferred, not reported.** `search()` swallows
  `BudgetExhausted` once it has any results (V0.3), so a truncated page set is
  indistinguishable from a complete one. The collector checks
  `budget.status()["remaining"] <= 0` after each sweep query and skips
  `record_sweep` if exhausted. Known false positive: a genuinely complete
  sweep that exhausts the budget on its last page is also skipped. Costs one
  hour of resolution; the opposite error corrupts lifespans.
- **`record_sighting` writes `listing_fields["spec_status"]` on insert,
  defaulting to `'pending'`** (V0.5 hardcoded `'stale'` and ignored the
  caller — corrected). The collector passes nothing, so every row it writes is
  `'pending'`. The update path still sets `'stale'` on a title change.
- **The collector owns one connection** for the process lifetime and passes it
  to every call. `DailyBudget` keeps its per-call pattern. `sqlite3` calls
  block the event loop; at this volume that is correct. Do not add
  `aiosqlite`.
- **Startup does not reconcile downtime.** Absence that was not observed is
  not absence; the first sweep after a restart resets `miss_count` on
  everything it sees.
- `AUCTION` removed from the profile's `buyingOptions`. Auction-only listings
  have no `price` field and cannot map, and design.md §5.5 rules a current bid
  out of baselines anyway. Provisional answer to §5.5's V0.8 question;
  reversible.

Live verification after ~2h: 1,026 listings / 1,028 observations (two real
revisions across ~18,000 sighting comparisons), one `'stale'` from a genuine
seller title edit, `miss_count` accumulating only on listings the AUCTION
filter change orphaned, zero `gone_at`, zero cycle errors.

Next: V0.7 ThinkPad T14 profile + normalize engine.

## Open items before V0.7

- **Persist raw before mapping.** The collector maps first and drops failures,
  contrary to design.md and this file's own trap entry. ~8 listings per sweep
  are lost this way. A mapping failure should write a `listings` row and an
  `observations` row carrying `raw_json` with null derived fields. Scoped fix,
  do it before V0.7 accumulates more history.
- **~8 fixed-price listings per sweep fail to map on a missing `price`.**
  Stable count, not growing. Not auctions — those are filtered out now.
  Suspect `priceDisplayCondition` (MAP / see-price-in-cart). Identify the
  actual shape before writing extract rules.
- **Correct design.md §7's budget math.** It assumes ~2 calls per cycle
  against a 150-listing active set. Measured: ~1,000 active listings, sweep
  costs ~11 calls, ~264/day for one watch. Still comfortable at 4,750, but the
  old figure would badly under-estimate five watches.
- **Raise `search.filters.price` in `profiles/thinkpad-t14.yaml`.** The search
  filter is the only lossy stage — anything above the ceiling is never fetched
  and cannot be backfilled. Gen 4/5 machines exceed $1200.
- Reject rules matching on `subtitle` are dead weight — Browse returns no
  subtitle. Remove or repoint at V0.7.
- Delete `reserve(n)`'s unused `n` parameter.
- **WAL high-water is ~4 MB** after the initial full sweeps. Checkpoints are
  clean (`wal_checkpoint(PASSIVE)` returns `(0, n, n)`). Re-check after a day
  of steady state; if it has grown an order of magnitude, something is holding
  a read snapshot.
