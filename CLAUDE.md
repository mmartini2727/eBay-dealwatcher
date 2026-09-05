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
│ ├── baselines.py (V0.8a) survival-derived candidates → percentiles
│ └── scoring.py (V0.8b) baselines/seeds → deal score, fallback ladder
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
- Two triggers, one path. The collector and scripts/backfill_normalize.py both normalize, and both build their input via normalize_input_fields() in normalize/listing.py. If one grows a new way of extracting fields from a raw dict, the other gets it too — they drifted once, and the backfill silently couldn't repair a class of row for a week.

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

**The sweep's pagination ceiling must exceed the active set.** A sweep that
stops paginating before it reaches every active listing never sees the
listings past that horizon — they get marked gone on the miss_count timer
even though they're still live on eBay. The symptom looks exactly like a
fast sale (`last_seen == first_seen`, a short or zero lifespan), which is
precisely the band the survival baseline weighs most heavily. `sweep_page_limit`
/ `sweep_max_pages` (`profiles/*.yaml`, `search.poll`) must be sized against
the actual active-listing count, not a guess — and the count grows, so a
value that's adequate today can silently become inadequate later. The sweep
coverage check in `run_sweep_cycle` (`engine/collector.py`) exists for
exactly that reason: it logs a WARNING when the sweep returns materially
fewer distinct items than `count_active_listings()` expects, so a repeat of
this doesn't have to wait for someone to notice the baseline looks wrong.
`scripts/repair_false_gone.py` clears rows shaped this way
(`last_seen == first_seen`) regardless of cause — but note the measured
never-swept rate did NOT change after raising the ceiling (see Current
status / the Resurrections open item), so pagination is a real, worth-
fixing risk in its own right, not a confirmed explanation for the rows
actually observed so far.

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
- Live database, 1,386 listings, all normalized: ok 677, rejected 502, partial 168, not_target 39. Zero pending, zero stale. The 5 former mapping-failure rows now normalize from raw fields via the backfill's fallback.
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
- V0.7c complete. Backfill gained the collector's raw-field fallback — normalize_input_fields() now lives in normalize/listing.py and both callers use it. AMD model-number patterns had the series digit made optional ([3579]?) so "Ryzen PRO 8540U" matches; the four-digit model number stays mandatory, so bare "Ryzen 5 PRO" is still None.
- **V0.8a complete: survival-derived baseline computation, no scoring yet.**
  `engine/baselines.py` derives one candidate per dead `spec_status='ok'`
  listing from its LAST observation only (design.md §2.1 — an earlier price
  point was ended by a cut, not a sale, and is not evidence about anything).
  `scripts/recompute_baselines.py` DELETEs+INSERTs the `baselines` table per
  profile; `scripts/baseline_report.py` is the actual deliverable — candidate
  pool breakdown, threshold sensitivity, per-bucket near-misses, and a
  falsification check on whether fast-selling prices are actually lower.
  Given reality (186 dead 'ok' listings, zero buckets at `min_samples=12`),
  this milestone is expected to write zero or near-zero baseline rows on
  first run — that's the correct outcome, not a bug. Could not run the
  report against real data in this checkout (`data/dealwatch.db` is empty
  here); verified end-to-end against synthetic fixtures instead — run it on
  the LXC for real numbers.
- **Found and fixed a real concurrency bug in `_apply_migrations` while
  building V0.8a's migration.** `ALTER TABLE ADD COLUMN` (no `IF NOT EXISTS`
  in SQLite) exposed a latent race: `DailyBudget` opens a fresh connection
  per call by design, and several threads hitting a brand-new database file
  at once could all decide to apply the same migration — the loser crashed,
  which hung `test_ratelimit.py`'s concurrency tests (a thread dying before
  it reaches a `threading.Barrier` leaves every other thread waiting
  forever). `_apply_migrations` now runs the whole read-version → apply →
  write-version sequence as one `BEGIN IMMEDIATE` transaction, statement by
  statement rather than via `executescript()` (which silently commits any
  open transaction before it runs, so wrapping it in `BEGIN IMMEDIATE`
  wouldn't have worked). Migrations 1 and 2's content is unchanged.
- **V0.7c: sweep pagination ceiling raised, coverage check added,
  false-gone repair script shipped — but the fix did NOT do what it was
  credited with.** The sweep's page size/depth moved from hardcoded
  `collector.py` constants (100 × 10 = 1,000) to `search.poll.sweep_page_limit`
  / `sweep_max_pages` in the profile (`PollConfig`, defaults 200 × 10 =
  2,000), and `run_sweep_cycle` now logs a WARNING when a sweep returns
  fewer than 95% of `count_active_listings()`'s count — both still correct,
  still worth having, and both still in place. **Correction (measured
  post-deploy):** the never-swept rate (`last_seen == first_seen` on dead
  listings) is flat at ~8% of daily deaths both before and after this fix,
  not improved by it. V0.7c's pagination-ceiling theory does not explain
  the never-swept rows after all — see the Resurrections open item below,
  reopened rather than treated as resolved. `scripts/repair_false_gone.py`
  and V0.8b's baseline-candidate exclusion (next bullet) are still correct
  responses to "these rows are unreliable," independent of what's actually
  causing them.
- **V0.8b complete: scoring engine.** `engine/scoring.py` resolves a
  baseline via a two-layer ladder — the `baselines` table (V0.8a) if a row
  exists for the exact `bucket_key`, else the best-matching `seed_baselines`
  entry (most matched keys wins, ties by file order) — and scores a
  listing's price against it. No bucket-coarsening step: partial seed
  matching already plays that role. `baselines.py` gained a second
  candidate-pool exclusion: a dead listing with `first_seen == last_seen`
  was never confirmed by a sweep, so its 0.0-lifespan is unmeasured, not
  fast (measured ~8% of daily deaths, avg. final price $378.95 vs. $362 for
  swept listings — not cheap fast-sellers). `best_offer_weight` deleted from
  the profile as dead config. `sanity_floor_pct` raised 25 → 35 (units: % of
  baseline p50) and now persists to `listings.sanity_flagged` (migration 4)
  as a flag, never a suppression. `scripts/score_active.py` scores every
  active `spec_status='ok'` listing and prints the best N by `ratio_to_p25`;
  not wired into the collector or FastAPI — V0.9 decides where scoring gets
  called from. Also: `_apply_migrations` now does a cheap plain-SELECT
  version check before ever taking a write lock, since `DailyBudget` opens a
  fresh connection per eBay call and was previously taking `BEGIN IMMEDIATE`
  on every single one just to confirm nothing needed applying; `_MIGRATIONS`
  is now a list of discrete statements per version instead of one blob
  string split on `;` at runtime (`_split_statements` deleted) — a future
  migration with a semicolon inside a comment or string literal would have
  silently mis-split against a populated production database.

## Open items before V0.8

- **Remaining ~166 partials: CPU marker with no model number and no ordinal**
  ("Core i5", "Ryzen 5"). Intel is derivable from generation + vendor, but that
  makes `bucket_require` satisfiable by inference, and a wrong generation would
  then manufacture a `cpu_family` too. Separate design session, not a pattern
  tweak.
- **Two disagreement listings produce impossible buckets** (`1|intel-11th`,
  `1|intel-12th`). V0.8 wants a sanity check on generation/CPU pairs that
  cannot exist.
- **Resurrections — reopened, unresolved.** Three cohorts observed pre-V0.7c
  (lifespan_mins 1082; 2836 ×7; 5670 ×3). Identical lifespans within a
  cohort are expected, not suspicious — last_seen is sweep-only and
  first_seen is usually a sweep, so hourly quantization makes matching
  values common. The 5670 cohort spanned two different sellers, ruling out
  batch-relisting. V0.7c's pagination-ceiling theory was plausible but is
  now contradicted by data: the never-swept rate is flat at ~8% of daily
  deaths both before and after raising the sweep ceiling, so pagination
  gaps do not explain these rows after all. Back to genuinely unresolved —
  possible causes not yet ruled out include eBay search-index reordering
  independent of page count, and something bucket/profile-specific. Do not
  draw conclusions about whether N=3 (the miss-count threshold) is right
  until the actual cause is identified; a fix aimed at the wrong cause
  won't move this number.
- Delete `reserve(n)`'s unused `n` parameter.
- **WAL high-water was ~4 MB** after the initial full sweeps. Re-check now that
  the backfill has written every row; if it has grown an order of magnitude,
  something is holding a read snapshot.
- `VACUUM INTO` refuses to overwrite an existing file. `rm -f` the target
  first, or the snapshot silently doesn't happen and you deploy without one.
