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
- Tests do not ship in the image. tests/ is deliberately not copied into the Dockerfile and pytest is not installed there — the Mac venv is the authoritative test environment. Do not add COPY tests ./tests or pip install '.[dev]' to the Dockerfile.

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

## Operational notes

- **Profile edits need `docker compose restart`; code changes need
  `docker compose up -d --build`.** `profiles/` mounts into the container,
  so a YAML edit is visible on disk immediately — but the process read it
  once at startup and keeps running against the old values until the
  container restarts. The failure mode is silent: `docker exec ... cat` or
  `grep` on the profile shows your edit is there, giving false confidence
  that it's live, while the running process is still scoring against
  whatever it loaded at boot. A plain `restart` is enough for a
  profile-only change and is the *required* step, not an optional extra; a
  full `up -d --build` is only needed when `dealwatch/` Python itself
  changes. See design.md §10 for the rest of the deploy notes.

---

## Testing

`pytest`. The network layer should be mockable — no test may require live eBay
credentials to pass. Live integration tests are allowed but must skip cleanly
when credentials are absent.

---

## Current status

- **V0.7b complete.** The normalize engine is wired to the database. The
  collector calls `normalize()` inline after every `record_sighting` (both the
  mapped path and V0.7a's raw-only mapping-failure path) and writes via
  `store_spec()`. `scripts/backfill_normalize.py` re-normalizes
  `pending`/`stale` rows using the same `store_spec()`; `--all` re-runs
  everything after a profile change. Idempotent — verified by running it three
  times against the same snapshot.
- Live database, 1,317 listings, all normalized: **ok 638, rejected 473,
  partial 166, not_target 35, pending 5.** The 5 pending are the mapping
  failures below; nothing is `stale` — the collector's inline normalize
  resolves a title change within the same sighting, so `stale` never survives
  past the sighting that produced it.
- Backfill output was verified against `scripts/normalize_report.py` over the
  same snapshot: identical distribution. Same engine, same input, so the write
  path adds nothing the pure function doesn't.
- **`buyingOptions` filter fixed.** eBay's set filters are **OR, not AND** —
  `[FIXED_PRICE, BEST_OFFER]` admitted `["AUCTION","BEST_OFFER"]` listings,
  which carry no `price` field (only `currentBidPrice`) and failed to map at
  ~5/sweep. Narrowed to `[FIXED_PRICE]`; BIN-with-Best-Offer still matches,
  auction+BIN still comes through with a real BIN price. Diagnosed from
  `raw_json` on disk — persist-raw earned its keep on its first sweep.
- **Profile edits need `docker compose restart`, not `up -d --build`.**
  `profiles/` is a read-only bind mount, so the file updates on disk while the
  running process keeps the profile it read at startup. `docker compose exec
  dealwatch grep ... /app/profiles/*.yaml` shows the *file*, not what the
  process is using — check the request URL in the logs instead. This cost an
  hour: the fix looked deployed and wasn't.
- Price ceiling raised to `[80, 2000]` and live-verified: 35 observations above
  $1,200 within two sweeps, including Gen 5 Ryzen 8540U, Gen 6 Ultra 258V, and
  Gen 6 Ryzen AI 350 — target machines that were invisible under the old
  filter. `alerts.max_price_usd: 1200` is the buying ceiling; the search filter
  is not.
- Accessory rule was rejecting Core Ultra machines on `webcam`; fixed, now 0
  hits. `category_ids: 177` does most of the accessory filtering.
  `for-parts-condition` reads 0 because `conditionIds` already excludes 7000.
  Both intentional — do not prune as dead weight.
- Generation disagreement (title text vs. CPU-implied): 479 agree, 2 disagree
  (~0.4%). Derive stays fill-null; do not add `overwrite: true` without new
  evidence.
- 153 distinct buckets; 15 ok-only reach `min_samples=12`; 35 are singletons.

## Open items before V0.8

- **Backfill can't normalize mapping-failure rows.** It calls
  `map_item_summary()` first, which throws, so those 5 rows stay `pending`
  forever. The collector handles the same case fine by reading title /
  condition_id straight from the raw dict. Two triggers, one store path, two
  different "what do I normalize from" paths — make the backfill fall back the
  way the collector does.
- **Remaining ~166 partials: CPU marker with no model number and no ordinal**
  ("Core i5", "Ryzen 5"). Intel is derivable from generation + vendor, but that
  makes `bucket_require` satisfiable by inference, and a wrong generation would
  then manufacture a `cpu_family` too. Separate design session, not a pattern
  tweak.
- **`Ryzen PRO 8540U` pattern gap.** No digit between "Ryzen" and "PRO", so
  `\bryzen\s*[3579]\s*(?:pro\s*)?8\d{3}` misses it. Unlike the bare-marker
  cases this one is fixable — the model number is right there.
- **Two disagreement listings produce impossible buckets** (`1|intel-11th`,
  `1|intel-12th`). V0.8 wants a sanity check on generation/CPU pairs that
  cannot exist.
- **Resurrections.** First observed on the V0.7a deploy (`lifespan_mins=1082`).
  Later, 7 items with identical `lifespan_mins=2836` (~47h) — most likely one
  seller's batch listed and pulled together, not index inconsistency.
  `gone_at` otherwise clusters in groups of 1–6 per hourly sweep, which is
  expected since only the sweep writes `last_seen`. §4.2 says count these;
  the running total is the only evidence about whether N=3 is right.
- **Correct design.md §7's budget math.** It assumes ~2 calls per cycle against
  a 150-listing active set. Measured: ~1,300 active listings, sweep costs ~11
  calls, ~450/day for one watch. Comfortable at 4,750, but the old figure would
  badly under-estimate five watches.
- Delete `reserve(n)`'s unused `n` parameter.
- **WAL high-water was ~4 MB** after the initial full sweeps. Re-check now that
  the backfill has written every row; if it has grown an order of magnitude,
  something is holding a read snapshot.
- `VACUUM INTO` refuses to overwrite an existing file. `rm -f` the target
  first, or the snapshot silently doesn't happen and you deploy without one.
