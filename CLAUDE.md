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
│ ├── scoring.py (V0.8b) baselines/seeds → deal score, fallback ladder
│ └── alerting.py (V0.9) evaluate() gates + run_alert_cycle() driver
├── notify/
│ └── discord.py (V0.9) embed builder + webhook POST, no laptop-specific knowledge
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

**SQLite history is irreplaceable.** Code is rewritable; three months of comps are not. See README → Database snapshots for the procedure.

**"No OS" is not barebones.** The `barebones` reject rule's alternation includes `os`, so "16GB 256GB NVMe No OS" — a complete machine missing only a Windows license — gets rejected. Refurb sellers list this constantly and those listings are systematically cheaper, so rejecting the class removes the low end of every bucket and biases medians upward. Fix at V0.7: drop `os` from that alternation and extract it as an attribute instead.

**Auction prices are not BIN prices.** For AUCTION listings `price` is the current bid; with both AUCTION and FIXED_PRICE it's the BIN and `currentBidPrice` is the bid. Auctions start near zero, so including them in bucket percentiles drags medians down and makes ordinary listings score as deals. They also always vanish on schedule, so lifespan says nothing about price. V0.8 must either weight by distance-from-end-date or exclude auctions from baselines — decide with real data, and consider dropping AUCTION from the profile's buyingOptions.

**Persist raw before mapping.** The collector writes `raw_json` first, then maps. A mapping failure becomes a row with raw data and null derived fields — re-mappable once the shape is understood. Mapping first and dropping failures loses history permanently: 5 of 150 live listings currently fail to map (auction-only, no `price` field), and that is 3% of comps gone for a bug that takes ten minutes to fix afterward.

**The sweep's pagination ceiling must exceed the active set.** A sweep that stops paginating before it reaches every active listing never sees the listings past that horizon — they get marked gone on the miss_count timer even though they're still live on eBay. The symptom looks exactly like a fast sale (`last_seen == first_seen`, a short or zero lifespan), which is precisely the band the survival baseline weighs most heavily. `sweep_page_limit` / `sweep_max_pages` (`profiles/*.yaml`, `search.poll`) must be sized against the actual active-listing count, not a guess — and the count grows, so a value that's adequate today can silently become inadequate later. The sweep coverage check in `run_sweep_cycle` (`engine/collector.py`) exists for exactly that reason: it logs a WARNING when the sweep returns materially fewer distinct items than `count_active_listings()` expects, so a repeat of this doesn't have to wait for someone to notice the baseline looks wrong. `scripts/repair_false_gone.py` clears rows shaped this way
(`last_seen == first_seen`) regardless of cause — but note the measured never-swept rate did NOT change after raising the ceiling (see Current status / the Resurrections open item), so pagination is a real, worth-fixing risk in its own right, not a confirmed explanation for the rows actually observed so far.

**A profile block authored in conversation is not deployed.** The V0.8b seed chart was written and reviewed in design discussion but never pasted into `profiles/thinkpad-t14.yaml`. The first scoring run silently used the old `match: {}` fallback for all 497 listings — no error, plausible output, uniformly wrong. To check which baseline actually applied, divide price by ratio; if every bucket gives the same number, the chart isn't live.

**Ad-hoc SQL runs on the host, scripts run in the container.** `python:3.12-slim` has the Python `sqlite3` module but not the CLI. `scripts/` isn't in the image either — `docker cp scripts/. dealwatch:/app/scripts/` first (the trailing `/.`/`/` forms, not the bare `docker cp scripts dealwatch:/app/scripts` — the bare form nests on a second invocation instead of overwriting; see README's Operational notes for the full command and its md5sum verification step).

---

## Operational notes

- **Three deploy modes, not two — match the mode to what actually changed.**
  `profiles/` mounts into the container, so a YAML edit is visible on disk
  immediately, but the process read it once at startup and keeps running
  against the old values until the container restarts. The failure mode is
  silent either way: `docker exec ... cat`/`grep` on the profile shows your
  edit is there, giving false confidence it's live, while the running
  process is still scoring against whatever it loaded at boot.
  - `docker compose restart` — profile-only changes (`profiles/*.yaml`).
    Cheapest, and the *required* minimum for a profile edit — not skippable
    just because `up -d --build` would also work.
  - `docker compose up -d` — `compose.yaml` or `.env` changes (a new env
    var, a port, a volume, a TZ setting). No image rebuild, but this
    **recreates** the container, not merely restarts it.
  - `docker compose up -d --build` — `dealwatch/` Python itself changed.
    Also a recreate, plus a fresh image build.
  - **Both non-restart modes wipe `/app/scripts/`.** It's `docker cp`'d in
    by hand, not part of the image or a bind mount (see below) — a
    container *recreate* is a new container, and `up -d` alone recreates
    one just as surely as `up -d --build` does. Re-run the `docker cp
    scripts/. dealwatch:/app/scripts/` step after **either**, not only
    after a rebuild.

  See design.md §10 for the rest of the deploy notes.
- **`python -m dealwatch.normalize.explain --profile <path> --title
  '<title>'`** — `explain.py` is a package module, not a standalone script;
  running it any other way (`python dealwatch/normalize/explain.py ...`,
  from the wrong directory) fails or silently imports the wrong thing.
  `-m` from the repo root is the only invocation that reliably works.

---

## Testing

`pytest`. The network layer should be mockable — no test may require live eBay
credentials to pass. Live integration tests are allowed but must skip cleanly
when credentials are absent.

---

### Current status

- **V0.7 complete.** Normalize engine wired to the database. Live (superseded by V0.8c's backfill, below — kept for history): 1,317 listings — ok 638, rejected 473, partial 166, not_target 35, pending 5.
- **V0.7c complete.** Sweep page size/depth moved to the profile (`sweep_page_limit: 200`, `sweep_max_pages: 10`); coverage warning fires when a sweep returns under 95% of the active count; `scripts/repair_false_gone.py` cleared 33 unreliable rows. See open items — this did not fix what it was credited with.
- **V0.8a complete.** `engine/baselines.py` derives one baseline candidate per dead `spec_status='ok'` listing from its **last observation only** (`design.md` §2.1 — an earlier price point ended in a cut, not a sale). `baselines` table, `listings.item_web_url`, `scripts/recompute_baselines.py`, `scripts/baseline_report.py`.
- **V0.8b complete.** `engine/scoring.py` resolves a baseline via a two-layer ladder: the `baselines` table for an exact `bucket_key`, else the best-matching `seed_baselines` entry (most matched keys wins, ties by file order). No coarsening step — partial seed matching plays that role. `sanity_floor_pct` 25→35 (% of p50), persisted to `listings.sanity_flagged`, flag never suppression. `best_offer_weight` deleted as dead config. `scripts/score_active.py` is script-only; V0.9 decides where scoring gets called from.
- **V0.8c complete.** `bucket_key` narrowed to `[generation, cpu_family, ram_tier]` — `storage_tier` dropped (design.md §2.1: it was the dominant source of `?` buckets and pure fragmentation cost, not a purchase discriminator). Bare-GB RAM extraction added (`ram_gb` no longer needs an adjacent ddr/ram/memory keyword). `tiers.ram_tier`'s 40GB break moved from `"64+"` to `"32"` (8 soldered + 32 SODIMM). Full backfill run: **1,480 listings — ok 718, partial 174, pending 0** (the 5 that were pending now normalize from raw fields; rejected+not_target is 588 combined). Post-backfill recompute produced the first two computed baselines: `2|intel-11th|16` n=24 p25 $215.00 p50 $244.90; `1|intel-10th|16` n=14 p25 $164.99 p50 $195.50.
- **Live scoring re-verified post-V0.8c — no longer "all from seeds."** Two buckets (above) now resolve from computed baselines instead of `seed_baselines`; every other bucket is still below `min_samples` and scores from seeds as before.
- **V0.8d complete: data integrity in the survival signal (design.md §4.4).** Two independent sources of fabricated lifespans, both confirmed from LXC diagnostics, not speculative. (1) **Pagination drift**: sweeps fetch ~1,200+ rows across 7 pages of 200 but yield only ~987 distinct items — eBay's relevance ordering re-ranks *between page requests*, ~6% per-sweep miss on a listing that's actually still active. `MISS_THRESHOLD` raised 3→5: predicted false-death rate drops from `0.06³×1000×22≈4.7/day` (which matched the observed resurrection-log rate of 4 over 21.8h) to `0.06⁵×1000×22≈0.0002/day`, a ~250× reduction, at close to zero cost since `record_sweep` already sets `gone_at = last_seen`. (2) **Multi-variation listings** (item_id `v1|<listing>|<non-zero variation>`) flap in and out of search results independent of the listing dying — 10 of 14 resurrection-log warnings traced to just 3 parent listings. New `listings.variation_id` column (`normalize/listing.py`'s `parse_variation_id`, migration 5) excludes these from baseline candidacy (`engine/baselines.py`); whether they're alertable is a separate V0.9 question, `score_active.py` untouched. New `sweeps` table (migration 5) makes the pagination-drift metric (`fetched_count - distinct_count`) a queryable time series instead of a log-line count, written on every sweep exit path. **Not yet run against production data in this checkout** — `backfill_normalize.py --all` + a recompute are needed before existing `baselines` rows reflect the variation exclusion; see design.md §4.4's explicit caveat that pre-V0.8d dead rows carry an unmeasurable number of undetected false deaths, since the resurrection count was always a lower bound, not a true rate.
- **Status: V0.8e — deployed and verified 2026-09-07T05:25:11Z.** `poll.sort` wired (poll only; sweep unaffected). Schema v5. See design.md's V0.8d correction (misses are correlated, not independent — the ~6% pagination-drift figure and its ~250× `MISS_THRESHOLD` payoff were both wrong, though raising it to 5 is still the right call) and §4.5 (the probe, the wiring, and this timestamp's lifespan-discontinuity consequence); §4.6 for other eBay API quirks found along the way; §4.7 for the open false-death-correlation question (candidate V0.8f) this surfaced.
- **Status: V0.9 complete — Discord alerts wired into the live collector (design.md §11).** `alerts:` block modeled in `schema.py` (`extra="forbid"`: a typo'd key is a startup error). Schema v6: new `alerts` table, one row per survivor per cycle, dry-run or not — not `last_alerted_at` columns, same "queryable history over an overwritten column" reasoning as the V0.8d `sweeps` table. `engine/alerting.py`'s `evaluate()` runs 8 gates (existence/active → spec_status → variation exclusion → usable price → buying ceiling → score → ratio trigger → cooldown/re-alert) and its `run_alert_cycle()` driver posts (dry-run: writes the row, skips the POST) via `notify/discord.py`, which carries zero laptop-specific knowledge — every rendered field comes from `alerts.title_template`/`alerts.fields`. `Collector.__init__` now calls `compile_profile`/`compile_seed_baselines`/`resolve_webhook_url` once at startup — a missing/empty `webhook_env` is a startup error, same class as a bad regex. Both `run_fast_poll_cycle` and `run_sweep_cycle` call the alert cycle after their own persistence work, wrapped in `except Exception: logger.exception(...)` — a Discord outage must never stall a poll or lose a sighting. **The dry-run-as-first-run-guard**: `last_alert()` ignores `dry_run` on purpose, so a day of dry-run rows gates cooldown/re-alert exactly like real ones — flipping `dry_run: false` does not fire on the ~1,000 already-active listings, only on genuinely new ones and real further drops. Also fixed in passing: `listings.item_web_url` was never written by the live collector (only backfilled once by `scripts/backfill_item_url.py`) — every listing collected from now on gets it at insert time, or every future alert embed would have no link. `profiles/thinkpad-t14.yaml`'s `alerts:` block completed (`max_price_usd` left at its existing 1200, not lowered to the design doc's 900 placeholder — see the YAML comment for why it's real headroom, not a no-op). Soldered-RAM buyability suppression pulled out to V0.9a during this milestone — see the §5.7 scope-change entry. **Live-verified 2026-09-08** — see design.md §11's dated addendum for the diagnostic detour (the `explain.py` no-match gap), the dry-run backlog's permanent-seen consequence, the sweep-timing correction, and the `whole-board` rule's final shape.
- **Seed chart edits, 2026-09-07 — profile-only, no rebuild, `dry_run: true` held throughout.** (1) `2|intel-11th|16` re-anchored 230/250 → 215.00/244.90, matching the post-backfill computed baseline (n=24) exactly — this is the chart's only ground-truth anchor and the reference point the rest of the chart is judged against. Note the computed `baselines` table already wins this exact bucket in live scoring (layer 1 beats layer 2), so this re-anchor is about consistency/fallback continuity, not a live scoring change today. (2) Added a "generation-only" bridge (`match: {generation: "N"}` for N=1–6, one per generation, priced as the plain average of that generation's two coarse family entries) between the family-level overrides and the flat `{}` catch-all — fixes the over-broad-catch-all finding from the same audit: a listing with a known generation but an unrecognized bare cpu_family (the ~166-partial class above) used to fall all the way to a flat 200/250 regardless of whether it was really a $200 Gen 1 or an $800 Gen 6 machine. `resolve_seed_baseline`'s most-matched-keys rule means a listing with a real cpu_family is unaffected. (3) RAM-tier "8" and "48" gaps found but deliberately left open — see the open item below.

### Open items

- **~166 partials from bare CPU markers** ("Core i5", "Ryzen 5"). Inferring Intel from generation + vendor would make `bucket_require` satisfiable by inference. Separate design session.
- **Fast-sale premise: an open question about the premise itself, not a pending measurement anymore.** Three buckets now have ≥5 fast and ≥5 slow candidates (report section (d)) — one of them shows the slow side cheaper, violating the premise in design.md §2.1. Not enough to conclude the premise is wrong, not enough to dismiss it as noise either.
- **`fast_lifespan_hours: 24` currently selects about half the candidate pool — a median split, not a tail.** Flagged next to the fast-sale-premise item above because it's the same underlying problem: if "fast" isn't actually isolating a distinct fast-selling population, a bucket where slow beats fast may be threshold noise rather than a real premise violation. Needs a look at the actual lifespan distribution, not a guessed hour count.
- **Seed chart: `ram_tier` "8" and "48" have no seed entry anywhere in the chart (audit, 2026-09-07).** Every 8GB listing falls through to whatever assumes "mid-spec 16GB" (the coarse generation+cpu_family entries, or a "32"-only override's absence); every 48GB listing falls through to the "32" override or coarse, with no premium either direction. Unlike the generation-only bridge added the same day (below), there's no real anchor to average from for either tier — any number here would be a guess with nothing under it. Deliberately left unfilled pending real fast-sale data for these tiers or a maintainer judgment call on the discount/premium shape, rather than inventing one now.
- **Gen 5/6 seeds: partially checkable now, still unvalidated.** `5|intel-ultra-1|32`, `5|amd-ryzen-8000|32`, `6|intel-ultra-2|32`, and `6|amd-ryzen-ai|32` each have 1–2 fast candidates — the first real data points against these seeds — but n is far too small to validate or reject anything yet.
- **Never-swept rate: 22 of 217 dead listings (10%), now with a matching-math explanation (V0.8d, design.md §4.4).** V0.7c's page-limit raise didn't explain it (the rate stayed flat before/after), but pagination *drift* does: eBay re-ranks results between page requests within a single sweep, not just across sweep-limit boundaries, at a measured ~6% per-sweep miss rate on a still-active listing. The independent-miss model at the old `MISS_THRESHOLD=3` predicts ~4.7 false deaths/day, matching the observed resurrection-log rate of 4 over 21.8h. `MISS_THRESHOLD` raised 3→5 in response. Confirm post-deploy that the never-swept/resurrection rate actually drops by roughly the predicted ~250× — the math matching once is encouraging, not proof the mechanism is fully understood. Resurrection cohorts (1082; 2836 ×7; 5670 ×3) are part of the same question — the 5670 cohort spanned two sellers, ruling out batch relisting.
- **`sweep_count` column** — the real fix for "never confirmed by a sweep," which `first_seen = last_seen` only infers.
- **CPU/generation validity check.** `4|intel-12th` exists in the data. Valid pairs belong in profile YAML; separate flag, `spec_status` untouched.
- **RAM plausibility check.** 4 listings land in `ram_tier` `64+` on Gen 1/2 hardware — at or beyond that hardware's real spec ceiling. Same shape as the CPU/generation validity item above: a profile-declared valid-combinations check, a separate flag, `spec_status` untouched.
- **`gone_at` can precede a listing's final observation.** Root cause: a
  fast poll can write a new observation (e.g. a price cut) without
  advancing `last_seen` (only a sweep does that); if death is detected
  before any sweep re-confirms the listing after that poll,
  `gone_at` ends up set from a `last_seen` that predates the poll's
  observation — a genuinely negative lifespan, not just the understated-
  by-up-to-an-hour case this was previously filed as. `baselines.py`'s
  negative-lifespan guard drops these safely (logged at WARNING, not
  silently discarded) rather than corrupting a baseline with a negative
  number — but the guard treating it as "shouldn't happen" is optimistic:
  observed once in the V0.8c 190-candidate measurement, and the
  interleaving is structural, not a fluke, so it will recur. Not yet
  designed; candidates are the same two floated in design.md §4.2 for the
  related understatement gap.
- **`test_concurrent_first_connect_against_a_fresh_file_does_not_crash_or_hang`
  is flaky under 20-way contention.** One run failed with
  `sqlite3.OperationalError: database is locked`; passed on immediate
  rerun with no code changes. Not the V0.8a deadlock regression this test
  was written to catch — that hung forever, this fails fast (`busy_timeout
  =5000` is set and doing its job). The `BEGIN IMMEDIATE` serialization
  itself is correct; this is an ordinary lock-wait timeout under unlucky
  scheduling with 20 connections queued on the same brand-new file.
  `DailyBudget` opens a fresh connection per call from many threads
  (`providers/ratelimit.py`), so this contention shape is the real
  production path, not a test artifact — worth a longer `busy_timeout` or
  a retry-on-`OperationalError` at `connect()`, not worth chasing as a
  correctness bug.
- **`Ryzen PRO 8540U` pattern gap** — no digit between "Ryzen" and "PRO".
- Delete `reserve(n)`'s unused `n` parameter.
- README operations section: snapshot command, host-vs-container tooling, restart vs rebuild.

