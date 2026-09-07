# DealWatch — Design

Authoritative document. If code and this file disagree, one of them is a bug.
Decisions here were made deliberately; do not relitigate them without reading
the reasoning first.

---

## 1. What this is

A generic marketplace deal-monitoring engine. It polls a marketplace for active
listings matching a profile, normalizes them into a comparable shape, scores
them against a price baseline, and alerts when something is worth acting on.

First target: Lenovo ThinkPad T14 on eBay. The architecture is profile-driven
and provider-driven specifically so the second target costs a YAML file and a
normalizer, not a rewrite.

**It is a headless daemon first and an MCP server second.** The alerting path
must never depend on an LLM being in the loop.

---

## 2. The central constraint: there is no sold-price API

eBay's `findCompletedItems` (Finding API) is deprecated. Sold and completed
listing data now lives behind the **Marketplace Insights API**, which is Limited
Release and effectively unobtainable for individual developers — applications
are routinely denied.

**Consequence: DealWatch cannot query sold comps. Do not design as if it can.**

Anyone (human or agent) picking this up will instinctively reach for sold
listings to build a price baseline. That path does not exist. The baseline has
to be constructed from data we collect ourselves.

### 2.1 How the baseline is built instead

Three layers, in order of when they become available:

1. **Seed baseline (day 0).** Hand-entered thresholds per bucket, sourced from
   the maintainer's own T14 generation/pricing chart and/or a one-time Terapeak
   export. Crude, but works immediately.
2. **Active-asking-price statistics (day 0).** Percentiles over currently listed
   prices for a bucket. Weak — asking prices are aspirational and skew high —
   but requires no history.
3. **Survival-derived baseline (week 4+).** The real one. Persist every listing seen, record every change to its price, and record when it stops appearing. **Lifespan is a property of a price, not of a listing.** A listing that sat at $340 for eleven days, was cut to $310, then cut to $290 and vanished the next morning is three observations: two slow prices and one fast one. The thing we want to know — what price gets sniped — is answered by the last of those, and storing only the listing's endpoints would file the whole 23-day lifespan against $290 and conclude the opposite.

   That error is directional, not noisy. Every price cut becomes evidence that the reduced price was slow to sell, which is exactly backwards. This is why listing history is split across two tables — see §4.1.

   The distribution of prices for fast-disappearing *observations* in a bucket is a better answer to "what price gets sniped" than sold comps would be, because that is the question we actually care about.

  - Measured (V0.7, n=1,094): layer 3 will not cover the buckets that matter most. Normalizing the collected set produces 139 distinct buckets, of which 14 reach scoring.min_samples=12 and 33 are singletons. The dense buckets are Gen 1–3 Intel; Gen 5/6 AMD — the actual buying target — appear in ones and twos. Bucket fragmentation is driven by nulls in ram_tier/storage_tier, not by the key being too wide, so this does not improve much with time.

  - Consequence: the survival-derived baseline is in practice a Gen 1–3 Intel feature. Gen 5/6 will run on seed baselines indefinitely. That is what layer 1 is for, but V0.8 must not be designed as if layer 3 eventually replaces it — the fallback is the steady state for the target generations, and the seed chart's accuracy matters more than this document originally assumed.

  - **Storage tier dropped from bucket_key (V0.8c).** `bucket_key` is now
    `[generation, cpu_family, ram_tier]` — no longer four fields. Measured
    on the LXC, after the V0.8c bare-GB RAM extraction fix, against 190
    baseline candidates:

    | | 4-field key (old) | 3-field key (new) |
    | --- | --- | --- |
    | clean candidates | 140 | 188 |
    | candidates with `?` | 50 | 2 |
    | distinct buckets | 40 | 30 |
    | buckets reaching `min_samples` | 1 | 2 |

    The original hypothesis for this drop was that `bucket_key`'s own
    width was a second, independent fragmentation source alongside null
    extraction (§2.1's earlier measurement). That turned out to be a minor
    effect in practice: 48 of the 50 remaining `?` candidates were
    storage-only, but the dominant cause of the *null* extraction problem
    was the RAM regex gap (bare "16GB 256GB" titles with no `ddr4`/`ram`/
    `memory` keyword), fixed the same milestone — not `bucket_key`'s width.
    Storage is also not a purchase discriminator for this target: two
    otherwise-identical machines differing only in storage size are not
    different deals, so there was no decision-quality cost to dropping it,
    only a fragmentation cost to keeping it. `storage_tier` stays defined
    in `tiers` and still lands in `spec_json` — only its `bucket_key`
    membership was removed, so it remains queryable and could be
    re-added later.

    This invalidated every stored `bucket_key` and required a full `--all`
    backfill re-run (`scripts/backfill_normalize.py`) before the recompute
    meant anything — a partial rollout (new listings keyed on three
    fields, old rows still keyed on four) would have silently corrupted
    every affected bucket's percentiles rather than failing loudly.
    Reversing this decision (re-adding `storage_tier`) is possible but
    costs another full backfill; it is not a config flag to flip back.

  - **Seed chart validated against computed data at two independent
    points (V0.8c).** The survival-derived baseline (layer 3) reaching
    `min_samples` for the first time gave the first opportunity to check
    the hand-authored seed chart (layer 1) against reality, not just
    against itself:

    | bucket | seed p25/p50 | computed p25/p50 (n) |
    | --- | --- | --- |
    | `1|intel-10th|16` | $160 / $200 | $164.99 / $195.50 (n=14) |
    | `2|intel-11th|16` | $230 / $250 | $215.00 / $244.90 (n=24) |

    Both are close — single-digit-percent p50 differences — which is
    reassuring given that Gen 5/6 AMD, the actual buying target, will run
    on seed baselines indefinitely (per the Consequence bullet above) and
    has no computed data to check itself against. `2|intel-11th|16`'s seed
    was anchored on a stale computed p50 of $249 from before this
    recompute; re-anchoring it to the new $244.90 is a deliberate
    follow-up, not done here, since the *post-backfill* recompute is what
    the anchor should be pinned to and that hadn't run yet when the seed
    file was last edited.

**Known weakness of (3):** disappearance conflates *sold* with *ended early* or
*pulled by seller*. `getItem` on a dead listing errors and does not disclose
which.

An earlier version of this document proposed weighting by how far before the
scheduled end date a listing vanished. **That mitigation is not available.**
Browse search returns `itemEndDate` only for auctions — measured live, 143 of
145 listings had no end date, because fixed-price listings are Good 'Til
Cancelled and have no scheduled end. Auctions do have one, but they always end on schedule, so the signal is worthless precisely where it exists. Per-listing `getItem` would cost the entire daily budget.

**Decision: accept the noise.** Raw lifespan is still signal — 90 minutes vs.
three weeks separates priced-to-sell from aspirational, whatever the reason for disappearance. If a discriminator is needed later, seller relisting the same title within hours is the most promising candidate. This is a deal finder, not an appraisal service.

Resolution note: absence can only be established by the hourly sweep (§7), so
lifespan resolution is one hour. A listing that appears and sells in twenty
minutes records as ≤1h. Adequate for separating priced-to-sell from
aspirational; finer resolution costs rate budget.

### 2.2 Implication for build order

The survival baseline needs weeks of accumulated history before it means
anything, and **that clock only starts when the collector begins persisting
rows.** Therefore the collector ships before scoring, before profiles are
finalized, before alerting. See §6.

---

## 3. Architecture

```
                        Internet
                           │
                           │ eBay only, one GET + rare POST
                           ▼
              Cloudflare Worker  (separate repo)
              ebay-deletion-endpoint
                           │
                        (no link)
                           │
┌────────────── Docker LXC (LAN / WireGuard only) ──────────────┐
│                                                                │
│   dealwatch                                                    │
│   ├── collector      poll → normalize → persist                │
│   ├── scoring        bucket baselines → deal score             │
│   ├── notify         Discord webhook                           │
│   ├── SQLite         listing history (the irreplaceable asset) │
│   └── MCP server     streamable HTTP, LAN only                 │
│                                                                │
└────────────────────────────────────────────────────────────────┘
```

### 3.1 The compliance endpoint is NOT part of this service

**Decision: eBay's Marketplace Account Deletion endpoint lives in a separate
Cloudflare Worker, in its own repo.**

Reasoning:

- It is a **permanent uptime obligation**. eBay re-verifies periodically; if it
  stops answering, warning emails follow and the production keyset can be
  disabled.
- It shares *nothing* with DealWatch — no database, no eBay credentials, no
  OAuth, no business logic. It needs a verification token and a URL string.
- Coupling it to the service under active development means every rebuild takes
  down a compliance endpoint. Fifty redeploys between V0.2 and V1.0.
- The homelab has no UPS. A power blip becomes an eBay warning email.

Moving it out means **no part of DealWatch is internet-exposed**. Bind the app
to LAN/loopback. No tunnel, no public hostname, no WAF policy to reason about.

Do not "simplify" by folding the endpoint back into the FastAPI app.

### 3.2 Worker implementation notes

- Response format: `{"challengeResponse": "<hex>"}`, HTTP 200,
  `Content-Type: application/json`.
- Hash is `SHA-256(challengeCode + verificationToken + endpointURL)`, in that
  exact order, returned as **lowercase hex digest — not base64**. eBay's prose
  documentation is misleading on this point; the code sample is correct.
- `endpointURL` must be a configured constant that is byte-identical to the
  string entered in the developer portal. **Never derive it from
  `request.url`** — that includes the query string and will silently produce a
  different hash.
- Verification token: 32–80 chars, generated once, stored as a Worker secret.
- POST handler: return 2xx unconditionally. Forward to Discord for visibility.
  **Do not persist the payload** — it contains eBay user identifiers, and we
  store no eBay user data, so acknowledgement is the entire obligation.
- Bind a custom domain on the existing Cloudflare zone. Keep it off the
  internal `home.` prefix.
- **Cloudflare Access must not cover this hostname.** eBay's challenge GET will
  receive an OTP login page and validation fails with no useful error. Same for
  Bot Fight Mode — the request will not look like a browser.
- Verify from outside the network (cell data) *before* saving in the portal.
- The production keyset stays disabled until validation passes.

---

## 4. Data model

### 4.1 Tables

```sql
watches(id, name, query, filters_json, normalizer, enabled)

-- identity + current state. One row per item_id, updated in place.
listings(
  item_id       TEXT PRIMARY KEY,
  profile_id    TEXT NOT NULL,
  title         TEXT NOT NULL,          -- current; a change re-triggers normalize
  seller        TEXT,
  seller_feedback_pct REAL,
  seller_feedback_score INTEGER,
  condition_id  INTEGER,
  spec_json     TEXT,
  spec_status   TEXT NOT NULL,   -- pending | ok | partial | rejected | not_target | stale
  reject_rule_id TEXT,
  bucket_key    TEXT,
  first_seen    INTEGER NOT NULL,
  last_seen     INTEGER NOT NULL,       -- heartbeat; SWEEP ONLY
  miss_count    INTEGER NOT NULL DEFAULT 0,
  gone_at       INTEGER,                -- = last_seen, not detection time
  lifespan_mins INTEGER
)

-- append-only. One row on first sight, one per watched-field change.
observations(
  id                INTEGER PRIMARY KEY,
  item_id           TEXT NOT NULL REFERENCES listings(item_id),
  observed_at       INTEGER NOT NULL,
  price_cents       INTEGER,
  shipping_cents    INTEGER,            -- NULL = unknown, 0 = free
  total_cents       INTEGER,
  buying_options    TEXT,
  current_bid_cents INTEGER,
  bid_count         INTEGER,
  raw_json          TEXT NOT NULL
)

-- survival-derived, V0.8a. Fully recomputable; scripts/recompute_baselines.py
-- DELETEs and re-INSERTs every row for a profile rather than updating in place.
baselines(
  profile_id    TEXT NOT NULL,
  bucket_key    TEXT NOT NULL,
  n             INTEGER NOT NULL,     -- fast-population count, not the dead count
  n_price_only  INTEGER NOT NULL,     -- of n, how many used price_cents (total_cents NULL)
  p10_cents     INTEGER NOT NULL,
  p25_cents     INTEGER NOT NULL,
  p50_cents     INTEGER NOT NULL,
  fast_hours    INTEGER NOT NULL,     -- the fast_lifespan_hours this row was computed with
  computed_at   INTEGER NOT NULL,
  PRIMARY KEY (profile_id, bucket_key)
)
alerts(item_id, watch_id, sent_at, price_at_alert)
```

Indexes: `observations(item_id, observed_at)`, `listings(bucket_key, gone_at)`.

- `PRAGMA journal_mode=WAL` at init. The collector writes while the MCP server
  reads; WAL keeps readers from blocking.
- **Money is integer cents.** Floats accumulate rounding error across
  percentile math and "N% below last alert" comparisons.
- `shipping_cents` NULL means **unknown**, not free. Free shipping is `0`.
  ~15% of live listings carry no shipping cost. **Revised at V0.8a:** a NULL
  `total_cents` is no longer excluded from baseline computation. It falls back
  to `price_cents` instead, and the row is counted separately
  (`baselines.n_price_only`) rather than dropped — an unresolved shipping cost
  says nothing about whether the *price* itself is a valid comparison point,
  and dropping it discarded real signal for no benefit. An unparseable spec
  (§5.2) is a different case and is still excluded outright: there's no
  fallback for "which bucket does this belong to" the way there is for
  "what did shipping actually cost."
- **`raw_json` lives on the observation, not the listing.** Sellers edit titles.
  When that happens the stored `spec_json` silently describes a machine the
  listing no longer claims to be. `title` is therefore a watched field: a change
  writes an observation *and* re-triggers normalization. `raw_json` per
  observation is the input that produced each spec — that is the re-runnability
  that matters, not price history, which normalization never reads.
- Dedup on `item_id`. **Re-alert when price drops materially below the price we
  last alerted at** — sellers revise BINs downward and that is frequently the
  actual deal.
- **`spec_status` vocabulary.** `pending` — never normalized (written by the
  collector at V0.6, before the engine exists). `ok` — normalized, all
  `bucket_require` fields present. `partial` — normalized, some fields null;
  alertable but excluded from baselines (§5.2). `rejected` — matched a reject
  rule; `reject_rule_id` names which. `not_target` — parsed as a different
  machine. `stale` — was normalized, but the title has since changed and the
  stored `spec_json` describes a listing that no longer exists.

  `pending` and `stale` both mean "V0.7 must normalize this," but they are not
  the same state: `pending` has never had a spec, `stale` has one that is
  wrong. Collapsing them loses the ability to tell a first pass from a
  re-parse, which is the only evidence you get about how often sellers edit
  titles. 

### 4.2 Disappearance rules

These are load-bearing. Getting them wrong produces a database that looks correct and is not.

- **Only the sweep writes `last_seen`.** The 5-minute poll uses
  `sort=newlyListed` with an `itemStartDate` filter and returns only what is
  new. A listing absent from that result set has told you *nothing*. If the
  collector treats fast-poll absence as absence, it will mark every existing
  listing gone within five minutes of starting — and the rows will still land
  and the lifespans will still compute.
- **N consecutive sweep misses before `gone_at`.** eBay's search index is not
  perfectly consistent; listings drop out of a sweep and return. Setting
  `gone_at` on first absence manufactures short lifespans, which land in exactly
  the bucket the survival baseline weighs most heavily. `miss_count` increments
  on a sweep miss, resets to 0 on any sighting, and `gone_at` is set at
  N=3.
- **`gone_at = last_seen`, not detection time.** Otherwise every lifespan
  carries the full detection delay (N sweeps ≈ 3h) as a constant error.
- **Resurrection means N is too low.** If an item with `gone_at` set ◊reappears,
  clear `gone_at`, `lifespan_mins`, and `miss_count`, and log at WARNING. A
  relist normally gets a new `item_id`; the same one returning is index
  inconsistency that outlasted the threshold. Count these — they are the only
  evidence you get about whether N=3 is right.
- **Known gap: fast-poll observations understate lifespan by up to one sweep
  interval.** Only a sweep advances `last_seen` (above), but a fast poll can
  still write a new `observations` row when a watched field changes — most
  importantly a price cut — without touching `last_seen`. If that poll-caught
  cut turns out to be a listing's last price before it disappears, the
  survival baseline (§2.1) derives its lifespan as `gone_at` (`= last_seen`
  from the sweep that last confirmed the listing) minus that observation's
  `observed_at` (the poll's timestamp, up to ~one sweep interval earlier) —
  understating true lifespan by up to ~60 minutes. Noise against
  `fast_lifespan_hours: 24`; not noise against a 2-hour threshold, where an
  hour is half the window. No fix designed yet. Two candidates, both of which
  change what `observations.observed_at` means and neither of which is free:
  (a) let a confirming sweep bump the existing last observation's timestamp
  forward instead of leaving it at the poll's time, or (b) derive lifespan
  from the earlier of (last observation's `observed_at`, the following
  sweep's `last_seen`) rather than the observation alone.
- **Honest limitation: `first_seen = last_seen` conflates two different
  things (V0.8b).** The survival baseline (§2.1) excludes any dead listing
  where `first_seen` equals `last_seen`, on the theory that it was never
  confirmed present by a sweep. That's the common case, but it isn't the
  only thing that produces the same equality: a listing that was, by pure
  bad luck, first sighted BY a sweep and then genuinely sold or vanished
  before the next sweep ran would show the identical `first_seen ==
  last_seen` shape, despite having been confirmed present exactly once.
  The exclusion can't tell "zero sweeps ever confirmed this" from "exactly
  one sweep confirmed this, coincidentally the same one that first saw
  it" - both collapse to the same two equal timestamps. The real fix is a
  `sweep_count` column that counts confirmations directly instead of
  inferring them from two timestamps colliding; deferred, because the
  false-exclusion case above is rare (it requires dying inside one sweep
  interval of being newly listed) and getting a real column into
  production correctly is a bigger change than this milestone's actual
  goal (excluding the common, high-volume case) needed to justify doing
  first.

### 4.3 The listing history is the irreplaceable asset

- Code can be rewritten. Three months of accumulated comps cannot. The SQLite file lives in the Docker LXC and is therefore in PBS, but SQLite inside a live LXC backup is not guaranteed consistent — take a periodic `VACUUM INTO` dump to the NAS as a second copy.

### 4.4 V0.8d — data integrity in the survival signal (2026-09-06)

Diagnostic work on the LXC found two independent sources of fabricated
lifespans in the survival baseline. Both are confirmed from production data,
neither is speculative.

**Finding 1 — pagination drift causes false deaths.** Each sweep fetches
~1,200+ rows across 7 pages of 200 but yields only ~987 distinct items,
because eBay's relevance ordering re-ranks *between page requests* — the
same item can land on two different pages of the same sweep (double-counted
in `fetched_count`, harmless) or fall through a page boundary entirely
(missed, not harmless). That's about a 6% per-sweep miss rate for a listing
that is actually still active. Modeling misses as independent per sweep, the
false-death rate at miss-threshold N is `0.06^N × active_listings ×
sweeps/day`. At the old N=3: `0.06³ × 1000 × 22 ≈ 4.7/day` — and the
resurrection-log warning rate measured over 21.8 hours was 4, matching the
independent-miss model closely enough to trust it. **Decision: raise
`MISS_THRESHOLD` 3 → 5** (`storage/sqlite.py`). At N=5 the model predicts
`0.06⁵ × 1000 × 22 ≈ 0.0002/day`, a ~250× reduction, at close to zero cost:
`record_sweep` sets `gone_at = last_seen`, not detection time (§4.2), so a
*true* death's recorded lifespan is byte-identical whether confirmed after 3
sweeps or 5 — the only cost is a longer delay (two more sweep intervals)
before the row finalizes.

**Finding 2 — multi-variation listings are not survival-signal material.**
Of 14 resurrection-log warnings, 10 came from just three parent listings.
eBay Browse item_ids have the form `v1|<listing>|<variation>`, where a
non-zero third component means the row is one variation of a
multi-variation listing (color, size, etc., sold as one eBay listing with
several buyable options). Which variation eBay happens to surface in a
given search result set is unstable — the row flaps in and out of results
independent of whether the underlying listing is actually still for sale —
and produces exactly the fabricated-lifespan pattern the survival baseline
is most sensitive to (observed: 962, 1854, 5607, 6378 minutes, none of them
real). **Decision: exclude any listing with a non-null `variation_id` from
baseline candidacy** (`engine/baselines.py`'s dead-listings query), via a
new `listings.variation_id` column populated at map/sighting time
(`normalize/listing.py`'s `parse_variation_id`), not by re-parsing item_id
at query time. Whether a variation listing should be *alertable* is a
separate, real question, deliberately left to V0.9's scoring — this
milestone only removes it from the baseline's input, `score_active.py` is
untouched.

**Also added: a `sweeps` table** (`storage/sqlite.py` migration 5), one row
per sweep cycle on every exit path (a completed sweep, a truncated one, and
the early-budget-exhausted return). Before this, the ~6% per-sweep miss rate
above had to be reverse-engineered from log line counts; `fetched_count -
distinct_count` now makes pagination drift a queryable time series instead
of a one-off measurement.

**Honest limitation, stated explicitly because it's easy to miss: pre-V0.8d
dead rows are contaminated by an unmeasurable number of undetected false
deaths.** The resurrection-warning count (14, above) is a *lower bound*
only — it counts listings that happened to re-enter the relevance window
before their `variation_id` (or, pre-fix, their pagination-drift miss
streak) crossed `MISS_THRESHOLD` a second time and got logged again. A
listing that never re-enters the window leaves no trace at all: its
fabricated lifespan sits in the baseline, indistinguishable from a real
fast sale, forever. Neither finding retroactively cleans existing
`baselines` rows — this milestone does not recompute or delete them (that's
a backfill + recompute decision for whoever runs it, informed by these two
fixes, not something to do quietly alongside the code change).

### V0.8d correction — the derivation was wrong, the decision was not

The `MISS_THRESHOLD` 3→5 change was justified by an independent-miss model:
a ~6% per-sweep miss rate, so 0.06^3 x 1000 listings x 22 sweeps/day = 4.7
false deaths/day, against 4 observed. The agreement looked like validation.
It was coincidence. Two errors, one compounding the other.

**Page-count error.** The 6% came from dividing 155 sweep requests by an
assumed 22 sweeps to get 7 pages per sweep, implying ~1,200 rows fetched
against 987 distinct. The 22 assumed the poll ran continuously; the container
was restarted repeatedly that session and each restart fires an immediate
sweep. The real figure is 6 pages per sweep and ~26 sweeps.

**Independence error, and this is the substantive one.** Direct measurement
from the `sweeps` table (14 sweeps, 2026-09-07) gives mean coverage 0.979,
worst 0.933, max duplicates 11 per sweep. First recorded row: fetched 1034,
distinct 1029, active_before 1039, truncated 0. So the real miss rate is ~2%,
not 6%. Substituted into the same model it predicts 0.024 false deaths/day
against ~2 observed — over-predicting by roughly two orders of magnitude when
given a correct input.

Misses are therefore **strongly correlated, not independent**: the same
listings are missed sweep after sweep, consistent with low-relevance items
ranking below the pagination cutoff every time. Confirmed post-V0.8e by
`v1|307026418852|0`, which missed five consecutive sweeps — a
three-in-a-billion event under independence.

`MISS_THRESHOLD = 5` remains correct: it costs nothing given
`gone_at = last_seen`, and it strictly reduces false deaths. But it is
**empirically justified, not arithmetically derived**, and it buys far less
against correlated misses than 0.02^5 would suggest.

**Pagination-drift hypothesis retired.** The earlier claim that
relevance-ordered pagination was causing the sweep to skip listings, and that
a stable sort key would stabilise it, is not supported. 5 duplicates in 1034
fetched rows. Pagination is fine. `truncated = 0` at 1034 items also confirms
the sweep is nowhere near the 2,000-item horizon, closing that concern.

Commits: `be63c18` (collector.py), `8266e16` (storage, normalize, baselines,
docs), `4fbdc07` (test hardening). Note that the first two are labelled
"Snapshot script implementation" and "Snapshot script for dealwatch"; the
commit messages do not describe V0.8d and `git log` will not lead here.

### 4.5 V0.8e — wire poll.sort

Deployed 2026-09-07T05:25:11Z (epoch 1788758711).

#### The gap

`profiles/thinkpad-t14.yaml` set `poll.sort: newlyListed`. `PollConfig` had
the field. `EbayBrowseProvider.search()` never sent it. The field was read by
nothing. Confirmed from production request URLs. The fast poll was fetching
page one of relevance ordering, not the newest 50.

#### Measured cost, before the fix

Discovery latency from `itemCreationDate` in stored `raw_json`, restricted to
listings created during the observation window (n=329):

| bucket | count | avg |
|---|---|---|
| <10m | 160 | 6 min |
| <65m | 158 | 34 min |
| <6h  | 7   | 96 min |
| <24h | 3   | 756 min |
| >24h | 1   | 2218 min |

Roughly half of new listings already landed on relevance page one unaided —
eBay gives new listings a temporary ranking boost. The other half waited for
the hourly sweep.

#### Probe, 2026-09-07 (decision gate before wiring)

Same query, two calls seconds apart:

```
--- unsorted (production behaviour at the time) ---
count returned : 50
total (envelope): 1033
itemCreationDate : n=50/50  min=2025-12-09T21:02:50Z
                   median=2026-09-01T22:20:37Z  max=2026-09-07T04:51:34Z

--- sort=newlyListed ---
count returned : 50
total (envelope): 1007
itemCreationDate : n=50/50  min=2026-09-04T15:34:09Z
                   median=2026-09-05T18:23:18Z  max=2026-09-07T04:51:34Z

symmetric difference of item_id sets: 74
```

eBay honours the parameter. Relevance page one spanned nine months; sorted
page one spanned 61 hours. The two sets shared only 13 items, so 37 of the 50
newest listings were absent from relevance page one — independent
corroboration of the latency histogram. Both lists shared the same max
creation date, confirming the new-listing relevance boost.

#### The change

`search()` takes a keyword-only `sort: str | None = None`, added to `params`
only when truthy. `run_fast_poll_cycle` passes `profile.search.poll.sort`;
`run_sweep_cycle` passes nothing. The sort field lives under `poll` and the
sweep must not inherit it — if `search()` read the profile itself, the
sweep's result ordering would silently change and entangle with the coverage
metric. Verified in production: poll URLs carry `sort=newlyListed`, zero
`limit=200` (sweep) URLs carry `sort=`.

#### Consequences

**Lifespan discontinuity at 2026-09-07T05:25:11Z.** Catching listings ~28
minutes earlier makes every post-cutover lifespan that much longer than a
pre-cutover measurement of the same true lifespan. Against
`fast_lifespan_hours=24` that is ~2%; against a listing that genuinely sells
in 90 minutes it is ~30%, and short-lived listings are exactly the population
the survival signal is built on. Any baseline recompute spanning this date is
mixing two measurement regimes.

**Poll page one is now "the 50 newest," not "the 50 most relevant."**
5-minute price-change resolution is lost on relevance-popular older listings;
they fall back to hourly via the sweep. Scoring uses the final observation, so
hourly is sufficient.

**Most poll cycles will now produce zero new observations.** In-band arrival
rate (price 80..2000) measured at ~4 per 11 hours, ~9/day, so a 50-item page
holds roughly five days of new inventory. Expected behaviour, not a
regression.

#### Early result

n=3 over 11 hours post-cutover, all under 10 minutes, avg 6 — the ~34-minute
band is empty. Directionally right but far too thin to conclude; at ~9
arrivals/day this needs three to four days.

Commits: `63716a9` (probe script), `aeb2956` (wiring: `search()`'s `sort`
keyword, `run_fast_poll_cycle`/`run_sweep_cycle`, `providers/ebay.py`'s
docstring).

### 4.6 eBay Browse API — empirical findings (2026-09-07)

Observations from the V0.8e probe and post-deploy verification that don't
belong to either milestone specifically — general facts about the API worth
not re-discovering later.

- **`total` is unreliable.** Same query and filters, two calls seconds apart:
  1033 unsorted, 1007 with `sort=newlyListed`. 26 listings cannot have ended
  in that window. `total` is approximate and varies with sort — it cannot
  serve as the denominator for the coverage check. (An earlier note suggesting
  it as the "honest denominator" is retracted.)
- **`sort=newlyListed` is honoured** and materially changes result
  composition: creation-date range on a 50-item page collapsed from nine
  months to 61 hours.
- **No hidden inventory.** `total` (1007–1033) sits against
  `active_count_before` 1039 and `distinct_count` 1029. The sweep sees
  essentially the whole matching set.
- **`query_exclude` is not wired.** `profiles/thinkpad-t14.yaml` sets it
  (`T14s`, `dock`, `palmrest`) but production request URLs show a bare
  `q=Lenovo ThinkPad T14`. Same never-wired pattern as `poll.sort`. Not yet
  triaged.

### 4.7 Open: false deaths from persistent sweep invisibility (candidate V0.8f)

~2 resurrections/day post-V0.8e, roughly half of them non-variation. These
are lower bounds — a listing wrongly marked gone that never re-enters the
sweep's visible window leaves no trace, and the per-observation storage model
records sightings only on change, so there is no per-sweep presence history to
reconstruct from.

Five non-variation false deaths examined: three `spec_status='ok'` with real
buckets (`1|intel-10th|16`, `1|intel-10th|32` x2), one `partial` (`?|?|16`),
one `rejected` (no bucket). Against a live-population base rate of 47.7% `ok`
(492 ok / 131 partial / 382 rejected / 27 not_target), `ok` listings are if
anything over-represented. The hypothesis that false deaths concentrate on
rejected non-target junk is **not supported** — they hit real targets,
including one of the two computed baselines.

The three `ok` cases carried discarded lifespans of 7691, 7871 and 9149
minutes (5–6 days), near the slow end rather than manufacturing fake fast
sales. One sample; the mechanism does not guarantee that shape.

Possible generation skew: the three `ok` cases were all intel-10th. If low
relevance for `Lenovo ThinkPad T14` correlates with older generations, false
deaths would concentrate in the buckets with the most history and least
buying interest, leaving the Gen 5–6 AMD targets cleaner. Testable, not
concluded — n=3.

**Raising the counter further is not the fix.** Five consecutive misses at a
~2% rate proves the misses are not independent. Two candidate approaches:

1. **Fetch the item URL directly before declaring death.** One API call per
   candidate, ~2/day against a 5,000/day budget. Gives a definitive live/dead
   answer instead of inferring from absence. Simpler and strictly more
   informative.
2. Record per-sweep presence to distinguish scattered misses from persistent
   invisibility. More storage and code, and it still only establishes
   invisibility, not death.

Option 1 is the intended approach.

**Cheap enabler for V0.9:** add `bucket_key` and last observed price to the
resurrection warning line. Two extra fields in a log line already being
written; in a month it yields a real sample instead of five rows.

`profiles/thinkpad-t14.yaml` has set `poll.sort: newlyListed` since it was
written, `PollConfig` has carried the field since `schema.py`, and
`EbayBrowseProvider.search()` has never sent it — confirmed from production
request URLs. This milestone wires it, after a probe confirmed it's worth
wiring, and is explicit about what kind of fix it is.

**Justification.** Measured discovery latency from `itemCreationDate` in
stored `raw_json`, 329 listings created during the observation window: 160
found in under 10 minutes (avg 6), 158 within the hour (avg 34), 11 later.
eBay's own temporary relevance boost for new listings already gets roughly
half of them onto page one unaided; wiring `sort` should move the other half
from ~34 minutes to ~6. **This is a discovery-latency improvement for V0.9
alerting, not a data-integrity fix.** An earlier hypothesis — that
relevance-ordered pagination was causing the sweep to skip listings, and
that a stable sort key would fix it — was retired by direct measurement in
§4.4's correction: the first `sweeps` row showed a 0.48% duplicate rate.
Pagination is fine. That reasoning is not reintroduced here.

**Step 1 — the probe, run on the LXC before any wiring:**

```
$ docker compose exec dealwatch python /app/scripts/probe_sort.py --profile /app/profiles/thinkpad-t14.yaml
query='Lenovo ThinkPad T14' limit=50

--- unsorted (today's actual behavior) ---
count returned : 50
total (envelope): 1033
first 10 item_ids: ['v1|298583199831|0', 'v1|257717273129|0', 'v1|178417406357|0', 'v1|178473657428|0', 'v1|407188103083|0', 'v1|366653482085|0', 'v1|278343885763|0', 'v1|137690368844|0', 'v1|287000104684|0', 'v1|137699583104|0']
itemCreationDate : n=50/50  min=2025-12-09T21:02:50+00:00  median=2026-09-01T22:20:37.500000+00:00  max=2026-09-07T04:51:34+00:00

--- sort=newlyListed ---
count returned : 50
total (envelope): 1007
first 10 item_ids: ['v1|820096971511|0', 'v1|820096917162|0', 'v1|188896714882|0', 'v1|298652066749|0', 'v1|287570145636|0', 'v1|137706685216|0', 'v1|206540356608|0', 'v1|377476959113|0', 'v1|336782104202|0', 'v1|287569679878|0']
itemCreationDate : n=50/50  min=2026-09-04T15:34:09+00:00  median=2026-09-05T18:23:18+00:00  max=2026-09-07T04:51:34+00:00

symmetric difference of item_id sets: 74
```

Outcome: eBay honors the parameter (the third of three possible outcomes —
see `scripts/probe_sort.py`'s docstring for all three). A symmetric
difference of 74 out of a possible 100 means only 13 of the 50 items
overlap between the two pages, and the sorted page's `itemCreationDate`
spread collapsed from ~9 months (2025-12-09 → 2026-09-07) to under 3 days
(2026-09-04 → 2026-09-07), with the median moving from 2026-09-01 to
2026-09-05. Not a no-op, not a rejection — proceed to wiring.

**Step 2 — wiring.** `EbayBrowseProvider.search()` gained a keyword-only
`sort: str | None = None`; `params["sort"]` is set only when `sort` is
truthy, so `None` produces no key at all rather than an empty value (only
the former was probed). `run_fast_poll_cycle` passes
`sort=profile.search.poll.sort`. `run_sweep_cycle` passes nothing,
deliberately — `poll.sort` is a fast-poll-only knob; if `search()` ever grew
a fallback to read `profile.search.poll.sort` itself instead of taking an
argument, the sweep's result ordering would silently change too, entangled
with the `fetched_count`/`distinct_count` coverage metric §4.4 just started
recording. `providers/ebay.py`'s module docstring — corrected in V0.8d to
say sort is never sent — is corrected again to describe the new behavior.

**Deploy timestamp — record here the moment this goes live:** `___`. This
is a lifespan-measurement discontinuity, not a formality: catching listings
~28 minutes earlier on average makes every post-fix lifespan that much
longer than a pre-fix one for the same true lifespan. Against
`fast_lifespan_hours=24` that's about a 2% shift; against a listing that
genuinely sells in 90 minutes it's closer to 30%, and short-lived listings
are exactly the population the survival signal weighs most heavily. Any
future baseline recompute spanning this date is mixing two measurement
regimes.

---

## 5. Normalization — the actual hard part

The eBay API is easy. Parsing eBay listing titles is not, and this is where the
project succeeds or fails. A naive price baseline over search results for
"ThinkPad T14" is garbage.

### 5.1 Hard rejects (must not reach the baseline)

- **T14s is not a T14.** Different machine, different price. Also check for
  `T14s Gen N`.
- **Barebones** — "no RAM", "no SSD", "no HDD", "no OS", "no drive". These will
  drag bucket medians down hard and generate a stream of false deal alerts.
- **For parts / AS-IS / cracked / bad battery / BIOS locked / no charger.**
  Frequently *not* in the title. **Browse search does not return a subtitle** — measured live, 145 of 145 listings had none, so any reject rule matching on `subtitle` is dead. Full descriptions require `getItem` per listing, which the budget cannot afford. Condition ID therefore carries more weight than this section originally assumed, and title-only matching is the practical ceiling for text rejects.
- **Lot listings** — "Lot of 5", "x5", "Bulk". One listing, N machines.
- **Accessories** — docks, palmrests, keyboards, LCD assemblies, motherboards.
  All match a keyword search for "ThinkPad T14".
- **Auction-only listings have no `price` field.** Browse returns
  `currentBidPrice` instead. Measured live, 5 of 150. They are not junk, but a current bid is not an asking price and must never be mapped as one. See §5.5. 

### 5.2 Attribute extraction

Parse generation, CPU family, RAM, storage, screen from the title into a `Spec`.
Gen 1 and Gen 2 exist in both Intel and AMD variants and they are not
interchangeable (Gen 1 AMD = Ryzen 4000, Gen 2 AMD = Ryzen 5000).

The cpu_family confidence isn't uniform: model-number matches are strong, ordinal matches ("12th Gen") are keyword-stuffable and are the only evidence when no model number is present. One of the two observed disagreements was a T14 listing carrying "Chromebook 12th Gen." V0.8 shouldn't weight the two sources equally.

Listings that fail to parse get `spec = unknown`: **excluded from baseline
computation**, but still eligible to be alerted on if the price is low enough to
be interesting regardless.

**Display is an extract field only — never in `bucket_key`.** Resolution,
panel type, and touch capability (`touchscreen` already extracts this way)
change what a listing is worth, but sellers report them inconsistently and
the values vary more continuously than RAM/storage tiers do. Adding display
to the bucket key would fragment buckets worse than `storage_tier` already
does (above), for a signal that's better handled as buyer judgment anyway.
Extract it, show it in the alert text, never score or bucket on it.

### 5.3 Sanity floor

**V0.8b: the threshold is 35% of the resolved baseline's p50, not 25%.**
Units are a percentage of p50 - 35 means "flagged when priced below 35% of
p50," i.e. a 65%+ discount. The original 25% figure meant a 75%+ discount
and effectively never fired in practice. "The resolved baseline's p50" is
whichever layer actually answered the ladder in §5.6 - computed or seed -
not always the survival-derived one.

This is a flag, never a suppression: the scorer sets `sanity_flagged` and
stops there. It does not block an alert, weight a score, or otherwise
decide anything - V0.9 reads the flag and decides. The flag is persisted
on `listings.sanity_flagged` (migration 4), not just logged, because
CLAUDE.md calls the sanity-floor queue a to-do list of missing reject
rules - a to-do list has to be queryable (`WHERE sanity_flagged = 1`)
later, not scrolled past in a log.

### 5.4 Best Offer

`buyingOptions` including BEST_OFFER means the listed price is an anchor, not a
transaction price. Weight accordingly; do not let it pollute baselines.

**V0.8b: `scoring.best_offer_weight` was dead config and has been deleted,
not implemented.** The search filter is FIXED_PRICE-only (§5.5/§7) and most
surviving listings carry BEST_OFFER anyway, so a weight applied to nearly
every row is a near-constant multiplier dressed up as a per-listing signal
- it would not have discriminated between listings, only rescaled all of
them together. `buying_options` is still recorded on every observation;
nothing currently reads it for scoring. "Weight accordingly" above remains
an open problem, not a solved one.

### 5.5 Auctions

`search.filters.buyingOptions: [FIXED_PRICE]` (§7 — eBay's set filters are OR,
so FIXED_PRICE must be the *only* value listed) excludes auction-only listings
at the API: a listing whose `buyingOptions` is `["AUCTION"]` never matches a
filter of `[FIXED_PRICE]`. Those are never fetched, so this section's earlier
open question — whether an admitted listing's `price` is a bid or an asking
price — is now closed at the API, not in code.

What still reaches the collector is `buyingOptions` including **both**
`AUCTION` and `FIXED_PRICE` (an auction with a Buy-It-Now fallback, or vice
versa) — such a listing still matches the filter on FIXED_PRICE alone. For
these, `price` is a genuine BIN and `currentBidPrice` is the separate,
live, in-progress bid. `Listing` records both and reconciles neither.
`price` is a legitimate baseline input here — it's a real asking price, not
a bid. `currentBidPrice` is not: it's an in-progress number that would drag
bucket medians toward auction opening prices, the same poisoning mechanism
as barebones listings from the opposite direction.

Open question for V0.8, narrower than it used to be: what to do with
`currentBidPrice` on an auction+BIN listing — ignore it (baseline off
`price` alone, which is already valid on its own) or use it as a secondary
signal (a live bid already above the BIN suggests real demand). Not a
blocking question the way "is this bid data safe to treat as a price" was.

### 5.6 The scoring ladder (V0.8b)

Given one listing, resolve a baseline in this order, and stop at the first
hit:

1. **The survival-derived baselines table row for this exact `bucket_key`**
   (§2.1, V0.8a), if one exists.
2. **The best-matching `seed_baselines` entry** - most matched `match` keys
   wins; ties break by file order (first wins). An empty `match: {}` block
   is the universal fallback and every profile needs one, or a listing that
   matches nothing more specific has no baseline to score against at all.

**There is deliberately no bucket-coarsening step between the two.** The
tempting middle layer - "if the exact bucket has no data, widen to just
`generation`+`cpu_family` and pool everything with any RAM/storage" - is
not built, on purpose: partial seed matching already plays that role, and
does it with hand-authored numbers a human chose for that specific narrower
match, not a wider, noisier pool assembled by relaxing the key.
Coarsening would also quietly reintroduce the exact fragmentation problem
§2.1 already measured (Gen 5/6 buckets are ones and twos) without the
human judgment seed_baselines was built to supply in that gap.

A `bucket_key` containing `?` can never hit layer 1 - V0.8a excludes those
from baseline computation entirely, by construction - and always falls
through to layer 2. That's intended, not a workaround.

**`seed_baselines` numbers are FAST-SALE prices, not market value - the
same quantity layer 1 computes.** They were authored as "at this price, it
gets sniped," deliberately matching what the survival baseline measures,
so the ladder can fall from one layer to the other without a conversion
factor anywhere. Applying one (e.g., treating seed p25 as a market-average
estimate needing a discount to become a "sniped" price) would make the two
layers answer different questions depending on which one happened to have
data for a given bucket - exactly the kind of layer-dependent inconsistency
a human reading an alert has no way to detect.

`baseline_layer` and `baseline_n` are carried on every score result and are
not decoration: without them there is no way to tell "real deal against 12
real observed sales" from "the seed chart's estimate for this bucket was
wrong." Collapsing the two into a single score number would discard exactly
the information a human needs to decide how much to trust it.

### 5.7 Buyability suppression (V0.9, pending)

A listing can match every spec requirement and still be a bad buy — soldered
RAM is the first known case: a machine whose RAM can't be upgraded is worth
less than an identical-spec machine with socketed RAM at the same price,
because the buyer is stuck with whatever shipped. V0.9 needs a way to
suppress or down-rank that class of listing.

**Decision: this must be a declarative rule, the same shape as reject/
require/extract (§5, `profiles/*.yaml`) — not Python that knows what a
laptop is.** A second profile (a different machine family, a different
manufacturer) has to be able to define its own buyability rules with a YAML
change, the same guarantee normalization already gives every other rule
class. Hardcoding "T14 Gen N solders RAM" into `dealwatch/` code would be
exactly the kind of target-specific Python module the profile system exists
to avoid.

**Decision: unknown RAM configuration still alerts.** A listing whose
`ram_tier` is `?` (extraction failed) must not be suppressed on the theory
that it might be soldered — that throws away a real deal to avoid a false
positive that was never confirmed. Flag it unverified in the alert text
instead, and let the buyer decide with the caveat visible.

**Open, blocking data question: which generations actually solder RAM is
not yet confirmed.** Gen 4 Intel and Gen 1/2 (both vendors) are the current
working guesses, not verified findings, and must not be encoded as a rule
until checked against Lenovo's own PSREF/spec sheets or a teardown source.
A wrong guess here is worse than the unknown-RAM case above: it suppresses
a real deal with false confidence rather than flagging honest uncertainty.

---

## 6. Build order

Deliberately sequenced. The collector comes before scoring because the data
clock is the long pole.

| Version | Deliverable |
| --- | --- |
| V0.1 | Docker + FastAPI skeleton. **Compliance endpoint ships as a Worker, separately.** |
| V0.2 | eBay OAuth (client credentials, token cache, refresh) |
| V0.3 | Browse API search + **rate-limit budget** |
| V0.4 | Normalized `Listing` model |
| V0.5 | SQLite + listing history |
| V0.6 | **Dumb collector loop — poll and persist, no scoring, no alerts** |
| V0.7 | Real ThinkPad T14 profile YAML + normalizer |
| V0.8 | Scoring engine |
| V0.9 | Discord alerts |
| V1.0 | MCP server (streamable HTTP) |

Every day the collector is not running is a day of comps that cannot be
recovered. Ship V0.6 early even if the normalizer is a stub — raw titles and
prices are still useful history and can be re-parsed later.

---

## 7. Rate limiting

Browse API default is **5,000 calls/day**, application-level, resetting at
midnight Pacific.

Build the budget tracker at V0.3, not later. Requirements:

- Persisted across restarts (a row in SQLite, not an in-memory counter).
- Hard stop with a reserve, not a soft warning.
- Exposed via `/health` and to the MCP server.

Polling strategy:

- `sort=newlyListed` with `filter=itemStartDate:[...]` so each poll returns only
  what appeared since the last one. Usually one page, one call.
- A separate slower sweep (hourly, deeper pagination) refreshes the full active
  set so disappearance tracking stays accurate.
- ~5 minutes is the useful polling floor — good ThinkPad deals are taken in
  minutes. Faster than that spends budget for little gain.
- **Budget math (corrected, V0.7c).** The original estimate here — "5
  watches × 288 polls/day × ~2 calls ≈ 2,900/day" — assumed a ~150-listing
  active set and ~2 calls per sweep cycle. Measured against the real active
  set (~1,300 listings, deep-paginating at `sweep_page_limit` ×
  `sweep_max_pages`), one hourly sweep costs ~11 calls, not ~2 — an active
  set roughly 9x larger than assumed. One watch (5-minute fast polls +
  hourly sweeps) measures at ~450 calls/day, comfortable against the
  ~4,750/day usable after `daily_reserve_calls`. Five watches at that
  measured per-watch rate is ~2,250/day — still comfortable — but only
  because the corrected per-sweep figure is used; naively scaling the old
  "~2 calls per cycle" formula to five watches would have looked fine
  while being wrong by ~5.5x on the sweep term, and a sixth watch or a
  larger active set is what would actually find the ceiling.

**eBay's `filter=` set-parameters are OR across every value listed, not AND —
undocumented in Browse's own reference, and it will bite again on the next
profile.** `buyingOptions: [FIXED_PRICE, BEST_OFFER]` does not mean "fixed-price
listings that also take offers" — it means "buyingOptions contains FIXED_PRICE
OR contains BEST_OFFER", which quietly admitted `["AUCTION","BEST_OFFER"]`
listings: an auction whose offer channel happens to include Best Offer, with
no `price` field at all, only `currentBidPrice` (see §5.5). Any future profile
that lists more than one value for a set filter needs to be checked against
this before assuming the extra value narrows results rather than widening them.

---

## 8. MCP server

**Transport: streamable HTTP, not stdio.** stdio servers are spawned by the
client, which does not work for a process living in a Docker LXC. HTTP lets the
server run alongside the collector and share the SQLite file directly.

Auth is WireGuard/LAN-only. That is a deliberate and adequate answer for a
homelab, chosen rather than defaulted into.

The MCP server is a **read-and-query interface over data the collector already
gathered**. It does not drive collection and it is not on the alerting path.

---

## 9. Things deliberately rejected

| Option | Why not |
| --- | --- |
| `driscoll42/ebayMarketAnalyzer` | eBay added CAPTCHAs; the author will not defeat them, so it is now a manual save-the-page-source workflow. Parsers are ~5 years stale. Useful to *read* for its query-exclusion and title-extraction ideas; not runnable as a component. |
| Existing eBay MCP servers as the engine | All wrap Browse (active listings only) — they do not solve the sold-data problem. MCP is also the wrong shape for a 24/7 headless monitor. `luke-nielsen/ebay-mcp` is worth importing as a *library* (`analysis.py` is pure functions; `client.py` has a working filter grammar and retry layer) but read `auth.py`/`client.py` before handing it credentials, and pin the audited commit. |
| Scraping `LH_Sold=1&LH_Complete=1` | Against eBay's user agreement; risks IP/account flags. Acceptable at most as a one-time baseline seed, never as the load-bearing data source. |
| eBay saved-search alerts | Not a replacement — no statistical baseline. Worth running in parallel as a latency backstop. |

---

## 10. Operational notes

- Secrets via `env_file:` pointing at a `.env` on the LXC filesystem — **not**
  inline in the Portainer stack editor. Portainer has previously reverted
  edited stack values on this host.
- Deploy from CLI compose, consistent with the rest of the stack.
- Add `/health` to Uptime Kuma. Separately, add an **external** monitor against
  the Worker's challenge endpoint — its silent death has consequences that
  otherwise go unnoticed for days.
- Container runs as a non-root user; `data/` is chowned to it.
- `profiles/` mounts read-only, `data/` read-write.
