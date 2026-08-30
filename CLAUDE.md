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

**V0.5 complete.** `storage/sqlite.py` extended with the `listings` and
`observations` tables and the `record_sighting`/`record_sweep` write path
(design.md §4.1, §4.2). No collector, scheduler, or normalize-engine wiring
yet — those are V0.6/V0.7.

- `record_sighting` inserts on first sight, otherwise compares the incoming
  values against **the most recent observation row**, in Python (`NULL !=
  NULL` in SQL, so a WHERE-clause comparison would flag every
  unknown-shipping listing as changed on every poll). Writes a new
  observation only on a watched-field change: `title`, `price_cents`,
  `shipping_cents`, `buying_options`. A title change also nulls
  `spec_json`/`bucket_key` and sets `spec_status = 'stale'` so V0.7
  re-normalizes it. Never writes `last_seen`.
- `record_sweep` is the only thing that writes `last_seen`. It increments
  `miss_count` on a miss, resets it to 0 on any sighting, and sets
  `gone_at = last_seen` (not detection time) once `miss_count` reaches
  `MISS_THRESHOLD = 3`. One transaction per sweep, not one per row.
- **`record_sighting`/`record_sweep` take `conn` as their first argument;
  `DailyBudget` (`providers/ratelimit.py`) opens a connection per call
  instead.** V0.6's collector owns one connection and passes it into every
  call — it must not open a fresh one per item the way `DailyBudget` does.
- **Rows from `storage.connect()` are `sqlite3.Row`, not tuples.** Use
  `dict(row)` when printing/logging one, and `row["column"]` for named
  access. Tuple-unpacking (`a, b = row`) still works, but `row == (1, 2)`
  does not.
- **Two clocks, one call site.** `map_item_summary(item, seen_at)` takes a
  tz-aware UTC `datetime`; `record_sighting`/`record_sweep` take integer
  Unix seconds. V0.6's collector is the one place both get called back to
  back — convert explicitly (e.g. `int(seen_at.timestamp())`), don't let
  one type drift in as the other's.
- **`total_cents` is not a `Listing` field — V0.6 has to compute it before
  calling `record_sighting`.** NULL `shipping_cents` must produce a NULL
  `total_cents`, never `price_cents + 0`. This is the one that silently
  biases baselines low: a listing with unresolved shipping would look
  cheaper than it actually is, and enough of those in one bucket drags the
  whole median down.
- **`buying_options` is a `list[str]` on `Listing`, `TEXT` in the DB.**
  `record_sighting` serializes it with `json.dumps` and compares the
  deserialized list back in Python. Serialize it the same way at every call
  site — a different serialization (sorted vs. insertion order, comma-join
  instead of JSON) makes two logically identical lists compare unequal, and
  change-detection fires a spurious observation on every single poll.

Next: V0.6 collector loop (poll → persist raw → map → persist).

## Open items before V0.6

- **Raise `search.filters.price` in `profiles/thinkpad-t14.yaml`.** The search
  filter is the only lossy stage — anything above the ceiling is never fetched
  and cannot be backfilled. Gen 4/5 machines exceed $1200. Everything
  downstream re-runs over `raw_json` and can be fixed later; this cannot.
- **Decide who owns the SQLite connection.** V0.6 runs a collector loop
  alongside FastAPI. `record_sighting`/`record_sweep` take a `conn`;
  `DailyBudget` opens one per call. A single connection shared between a
  background task and request handlers hits SQLite's threading rules. Settle
  this at the top of the V0.6 session, not in a traceback.
- Reject rules matching on `subtitle` in `profiles/thinkpad-t14.yaml` are dead weight — Browse returns no subtitle. Remove or repoint at V0.7.
- Delete `reserve(n)`'s unused `n` parameter.
