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

**Known caveat: pre-2026-09-07 baselines may include component listings
(2026-09-07).** Live dry-run scoring surfaced a bare T14 Gen 1 motherboard
(`v1|389916775126|0`, $112.95) as the single best deal in the active set —
it normalized to a whole-machine bucket (`1|amd-ryzen-4000|8`) and scored
against a whole-machine baseline because no reject rule caught it. Root
cause: `accessory`'s existing `motherboard`/`mainboard` terms (§5.1) were
being defeated by that same rule's `unless` clause, which exempts any title
mentioning a CPU marker — added to stop Core Ultra whole-laptop listings
from false-rejecting (see the `accessory` rule's own comment in
`profiles/thinkpad-t14.yaml` for that history) — but a board listing
routinely names its own onboard CPU, so the exemption was silently
admitting exactly the component listings those terms exist to catch.

**Correction to how that root cause was actually established**: an earlier
version of this entry cited `explain.py`'s trace as showing `accessory`
"matched but was suppressed by `unless:`" for the offending title. That
overstates the evidence — the trace actually printed a plain `no match` for
`accessory` on that title, which is exactly what it also prints for a rule
that never matched at all. `explain.py` does not currently distinguish
"the `any:` patterns never matched" from "they matched, and `unless:`
suppressed it" — both collapse to the same `no match` line. That ambiguity
is a real diagnostic gap in `explain.py` itself, and it is what sent the
initial investigation down the wrong path before the root cause above was
confirmed separately, by testing `accessory` against a bare title with the
CPU mention removed and observing it fire correctly. Fix `explain.py` to
print which branch a rule took (never-matched vs. matched-then-suppressed)
before trusting its trace on a similar case again.

Fixed with a new, unless-free `whole-board` reject rule
(`profiles/thinkpad-t14.yaml`) — see §11's addendum for why it ships with
only four board-synonym terms rather than the full component/chassis-part
candidate list originally drafted. Existing listings re-normalize on their
next sighting, so any component listing already in the database flips to
`spec_status='rejected'` on its next sweep — but its historical
observations remain, and were already included in any `baselines` row
computed before this fix landed. Those computed baselines may be slightly
depressed by an unknown number of component listings that were live at
recompute time. No action taken; this is a known caveat on existing
`baselines` rows, not a retroactive cleanup - the next `recompute_baselines`
run naturally excludes anything rejected by then.

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
  `sort=newlyListed` alone (V0.8e; no `itemStartDate` filter — see below) and
  returns a fixed-size page of the current newest listings, not "only what's
  new since the last poll." A listing absent from that page has told you
  *nothing* — it's a small, constantly-shifting slice of the active set, not
  the full one. If the collector treats fast-poll absence as absence, it will
  mark every existing listing gone within five minutes of starting — and the
  rows will still land and the lifespans will still compute.
- **`itemStartDate` filtering was designed and deliberately not built.** The
  original polling design here and in §7 called for `sort=newlyListed`
  combined with an `itemStartDate:[<last poll>..now]` filter, so each poll
  would return only listings created since the previous one. It was never
  implemented; V0.8e's probe-then-wire process built `sort=newlyListed` alone
  instead. Reason: a time-window filter needs a persisted checkpoint of "when
  did the last poll actually run" to compute its lower bound, and this
  collector deliberately keeps `CollectorStats` in-memory only (its own
  docstring: "a restart losing them is fine"). After a restart, a crash, or
  any other gap in the poll loop, a naive `itemStartDate` filter would either
  need that missing checkpoint or fall back to a fixed lookback window — and
  an outage longer than that window silently drops the listings created
  during it, with no signal anything was missed, because the very next
  poll's filter starts counting from *now*, not from before the gap.
  `sort=newlyListed`'s fixed-size, checkpoint-free page has no such failure
  mode: whatever the gap was, the next poll just sees "the current newest N,"
  and coverage self-heals with no state to lose. Do not re-add the date
  filter later without re-deriving this trade-off — it looks like a strict
  improvement (narrower, more relevant pages) and is actually a regression on
  outage recovery.
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

### 5.7 Buyability: label, don't suppress (V0.9c, pending) — scope change, 2026-09-07

Renumbered twice, neither time because of this section's own content.
V0.9a to V0.9b (2026-09-08): V0.9a itself went to multi-notifier support
instead (§11's dated entry). V0.9b to V0.9c (2026-09-09): V0.9b itself went
to the `?`-bucket alert gate and honest lifespan NULLs instead (§11's later
dated entry). This section's content and status are otherwise unchanged
both times - it is still pending, still blocked on the same PSREF check.

A listing can match every spec requirement and still be a bad buy — soldered
RAM is the first known case: a machine whose RAM can't be upgraded is worth
less than an identical-spec machine with socketed RAM at the same price,
because the buyer is stuck with whatever shipped.

This section originally framed the fix as suppression, blocked on an
unverified generation mapping. That framing fused two different things that
V0.9 (Discord alerts, §11) separated:

- *"Gen 4 AMD solders RAM"* is a fact about hardware. It is profile
  knowledge and belongs in `derive:` as a `ram_upgradeable` attribute — an
  existing generic stage (§5), no new rule language needed.
- *"I won't buy a soldered 16GB machine"* is a preference. It is not a
  property of the listing, and does not belong in the normalization
  pipeline at all.

**Decision: this must never become a `reject:` rule.** A soldered Gen 4 AMD
is a valid comp for other soldered Gen 4 AMDs — rejecting the class strips
real data out of a bucket that already has almost none (§2.1's
fragmentation concern applies here too).

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

**RAM configuration by generation (checked against PSREF, 2026-09-09) -
the blocking data question above is now mostly resolved:**

| Generation | Configuration |
| --- | --- |
| Gen 1/2 | Hybrid - 1 slot soldered, 1 SODIMM |
| Gen 3/4 | Fully soldered |
| Gen 5 | Dual SODIMM (fully upgradeable) |
| Gen 6 (AMD) | Dual SODIMM (fully upgradeable) |
| Gen 6 (Intel) | Depends on Arrow Lake vs. Lunar Lake - not distinguishable from `cpu_family` alone (both currently normalize to `intel-ultra-2`); needs the CPU model number, which this profile does not currently extract |

Gen 3/4 fully-soldered is the single biggest exposure (every listing in
that bucket ships with whatever RAM it has, no upgrade path at all), and
Gen 1/2's hybrid configuration means "soldered" isn't even a whole-machine
fact there - it's a statement about half the installed RAM. **No
suppression or labeling rule was built from this table in this milestone**
despite it resolving most of the previously-blocking uncertainty: current
alert volume in the Gen 3/4 bucket does not justify the implementation
cost yet, and Gen 6 Intel's model-number gap means any rule covering "every
generation" would ship with a known hole in it. This table is here so
whoever picks up V0.9c has the hardware facts on hand rather than
re-deriving them, not because a rule is imminent.

**V0.9c on hold, after that PSREF check:** add the `derive:` rule and put the
attribute in the alert body (`alerts.fields`, §11) — label, don't suppress.
If the label is wrong you see it and fix it; if a suppression rule is wrong
you never see the listing and never learn. Whether suppression is worth
building at all gets decided from a month of real alert rows, which the
`alerts` table (§11) now makes queryable.

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

- `sort=newlyListed`, no date filter (V0.8e — see §4.2's note on why an
  `itemStartDate` filter was designed and deliberately not built). Each poll
  fetches a fixed-size page of the current newest listings; at a measured
  in-band arrival rate of ~9/day (§4.5), a 50-item page holds roughly five
  days of new inventory, comfortably covering the interval between polls
  without a time-window filter. Usually one page, one call.
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

---

## 11. V0.9 — Discord alerts (2026-09-07)

V0.8b built the scoring ladder and explicitly left "where does scoring get
called from" open. This milestone answers it: scoring runs inside the live
collector, on every fast-poll and sweep cycle, and survivors get a Discord
embed.

### Where scoring is called from, and why it's failure-isolated

`Collector.__init__` compiles the profile once at startup -
`compile_profile`, `compile_seed_baselines`, and `resolve_webhook_url`
(`engine/alerting.py`) - the same "fail fast on a bad config, not on the
first listing that happens to reach the broken path" principle every prior
profile-driven stage already follows. A `webhook_env` naming an unset or
empty environment variable is a `ProfileCompileError` here, at container
start, not a silent no-op discovered days later when nothing has ever
posted.

Per-cycle, `run_fast_poll_cycle` and `run_sweep_cycle` each call
`engine.alerting.run_alert_cycle` once, **after** that cycle's own
persistence work is complete - after the poll's `record_sighting` calls,
after the sweep's `record_sweep`/`store_sweep_stats`. Both call sites wrap
it in a bare `try/except Exception: logger.exception(...)`. This is not
optional hardening: a bug in the embed builder, a Discord outage, or a
webhook 5xx must never stall a poll cycle or prevent a sighting write - the
collector's one job, since V0.6, is to not lose data, and alerting is
downstream of that job, never allowed to endanger it.

The sweep's early-exit paths (budget exhausted before any query, or
exhausted mid-sweep) both `return` before `record_sweep` runs, same as
V0.8d's `sweeps`-table bookkeeping - the alert cycle call sits after
`record_sweep`, so it goes with those early returns too. Alerting off a
possibly-truncated `seen_item_ids` would be no better founded than
`record_sweep`'s own absence-bookkeeping would be on it.

### The `alerts` table, and why it is a table, not columns

`last_alerted_at`/`last_alerted_price_cents` columns on `listings` would
answer "when did I last alert on this item" and destroy "what did this
system actually tell me last month, and was any of it right" - the only
way the scoring ladder (and the seed-baseline guesses in particular) ever
gets evaluated against reality after the fact. Same reasoning as the
`sweeps` table (V0.8d): a queryable history, not a log line nobody can
re-run a report against. `baseline_layer`/`baseline_n` are persisted on
every row for the same reason `ScoreResult` carries them (§5.6) - without
them there's no way to distinguish "real deal against 24 observed sales"
from "the seed chart's guess for this bucket was wrong," a month from now,
from this table alone.

### The dry-run-as-first-run-guard mechanism

`last_alert()` (`storage/sqlite.py`) returns the most recent alert row for
an item **regardless of `dry_run`**. This single decision does double duty.
`alerts.dry_run: true` is the shipped default - it writes a real row per
survivor, with `delivery_status='dry_run'`, but posts nothing. Because
cooldown/re-alert (`engine/alerting.py`'s `evaluate()`) reads `last_alert()`
without filtering on `dry_run`, every one of those dry-run rows immediately
starts gating future alerts on the same item exactly as if it had actually
posted. The practical consequence: flipping `dry_run` to `false` the next
morning does not fire on the ~1,000 already-active listings that would
otherwise all look brand-new to the cooldown check - only on genuinely new
listings and genuine further price drops from that point forward. No
separate backfill script exists for this, or is needed - a day of dry-run
operation before flipping the flag is the backfill.

**The cost of that guard, stated plainly so it doesn't read as a bug
report in six months:** every listing in the dry-run backlog is marked
seen **permanently**, not just for the first live cycle after the flip. A
listing that alerted once (dry-run or real) at $250 and never moves again
will never alert again at that same $250 - `cooldown_minutes` expires, but
`realert_drop_pct` still requires a further drop below the price already on
file, forever, for as long as that alert row remains the most recent one.
This is not a bug: re-alerting on an unchanged price is exactly the noise
the cooldown/re-alert gate exists to suppress. But it is the mechanism that
made a live system with the alert cycle running, gates all passing, and
`dry_run: false` set look like nothing was happening - every candidate
listing already had a row on file from the dry-run day, and an unchanged
price never clears the drop-required gate. Checking `alerts` table row
counts (any `delivery_status`, not just `'sent'`) is the correct way to
confirm the pipeline is alive; an empty Discord channel is not evidence of
a broken pipeline on its own.

### The poll-vs-sweep split, and a correction

The fast poll sees new listings (page one, `sort=newlyListed`, V0.8e); the
sweep sees price drops on everything else, because only the sweep
enumerates the full active set. **Correction to this file's earlier
framing**: §4.2's original text (before V0.8e) described the fast poll as
using `sort=newlyListed` **with an `itemStartDate` filter**, "so each poll
returns only what appeared since the last one." That filter was never
built - see §4.2's own "designed and deliberately not built" entry for why
(a persisted-checkpoint requirement that a stateless `sort=newlyListed`
page avoids entirely) - and this section's original text repeated that
stale description rather than the corrected one. The fast poll returns a
fixed-size page of the current newest listings; `run_alert_cycle` receives
whatever `item_ids` that cycle actually touched, new or not, and gates on
real data (spec_status, price, ratio, cooldown) rather than on the
fetch mechanism telling it something is new.

### V0.9c scope change (§5.7)

Soldered-RAM buyability suppression, originally scoped as part of this
milestone's target list, was pulled out during this milestone - see §5.7's
amended entry. Renumbered twice since: V0.9a to V0.9b (2026-09-08) once
V0.9a itself went to multi-notifier support instead (this file's V0.9a
dated entry below), then V0.9b to V0.9c (2026-09-09) once V0.9b itself went
to the `?`-bucket alert gate and honest lifespan NULLs instead (this file's
later V0.9b dated entry) - the content below is otherwise unchanged from
when it was written. The short version: suppression-via-`reject:` would
strip real comps out of buckets that already have almost none, and the
specific generation/vendor mapping needed to build it correctly has not
been verified against Lenovo's own spec sheets. Labeling (rendering the
attribute in the alert body once `derive:` produces it) ships in a future
V0.9c; suppressing anything on an unverified guess does not.

### Live-verification addendum (2026-09-08)

**Sweep timing is an interval loop from container start, not a wall-clock
schedule.** `_sweep_loop`/`_fast_poll_loop` (`engine/collector.py`) each run
their first cycle immediately when the task starts, then `asyncio.sleep`
for `sweep_interval_minutes`/`interval_minutes` before the next one - there
is no anchor to the wall clock (no "always at :00 past the hour"). Two
consequences worth having on file: the sweep fires immediately on boot,
not after waiting out its first interval, and **every container restart
resets the phase** - a sweep that had settled into running at :15 past the
hour will run at whatever minute the container happened to come back up at,
and stay there until the next restart. Nothing depends on sweep phase
today, but a future feature that assumes "the sweep runs on a stable
schedule" would be assuming something this loop does not guarantee.

**Diagnostic gap confirmed in `explain.py`.** See §2.1's corrected caveat
entry above - `explain.py`'s trace prints `no match` for a reject rule
whether its `any:` patterns never matched at all, or they matched and were
then suppressed by an `unless:` clause. Those are different facts and the
trace currently can't tell them apart, which cost real diagnostic time
tracking down the motherboard false-alert above. Not fixed in this
milestone; noted for whoever next touches `normalize/explain.py`.

**Open item: `accessory`'s `unless: \b(laptop|notebook)\b` is itself
over-broad.** It exists so a whole-laptop listing that happens to mention
an accessory word ("T14 laptop with dock included") isn't wrongly
rejected - but the bare word "laptop" anywhere in the title is sufficient
to trigger it, including inside a seller's own cross-listing boilerplate.
A real title observed during this verification pass: `"... Motherboard
Laptop ..."` - a seller describing a laptop motherboard, using "laptop" as
a descriptive adjective for what kind of motherboard it is, not asserting
the listing is a complete laptop. That title's `accessory` match on
`motherboard` gets exempted by the `unless:` clause for exactly the wrong
reason. The new `whole-board` rule (below) has no such `unless:` and
catches this specific title correctly regardless, so it is not an active
false-negative today - but `accessory`'s own `unless:` remains
over-broad for any future term added to that rule's `any:` list, and
should be tightened (e.g. requiring "laptop"/"notebook" to appear away
from a board/part term, not just anywhere in the title) before it is
trusted again on its own.

**Why `whole-board` shipped with four terms, not the thirteen originally
drafted.** The candidate list floated for the new rule (`profiles/
thinkpad-t14.yaml`'s Task 1 draft) included `palmrest`, `top cover`,
`bottom cover`, `lcd assembly`, `screen assembly`, `bezel`, `heatsink`,
`hinge`, and `ribbon cable` alongside `motherboard`/`mainboard`/`system
board`/`logic board`. Validated against live collected titles (Task 2),
every one of those nine non-board terms returned **zero matches** across
the active set; `motherboard`/`mainboard`/`system board`/`logic board`
were the only terms that actually appeared. Reason, not coincidence: this
profile's search is scoped to eBay category 177 (PC Laptops & Netbooks) -
bare chassis/cosmetic parts (a palmrest, a bezel, a heatsink) are
overwhelmingly listed under Laptop Replacement Parts or similar categories
instead, so they mostly never enter this profile's result set at the API
level, before any reject rule gets a chance to run. `accessory`'s existing
`palmrest`/`bezel`/`heatsink`/`hinge` terms are not proven dead code by
this - they may still be pulling weight against listings that entered via
`query_exclude`-missed or cross-category edge cases - but there is no live
evidence today that `whole-board` needs its own copies of them. Recorded
here specifically so nobody re-adds the dropped nine to `whole-board`
speculatively without re-running the same live-title check first.

### V0.9a — multi-notifier support (2026-09-08)

V0.9 shipped with exactly one delivery channel hardcoded into
`run_alert_cycle` (`discord.send_alert`, called directly). This milestone
generalizes that to a list, `alerts.notifiers` (`schema.py`'s
`AlertsConfig`), typed `list[Literal["discord", "pushover"]]` and defaulting
to `["discord"]` so a profile predating this field alerts exactly as it did
before. Dispatch is a `name -> module` dict (`engine/alerting.py`'s
`_NOTIFIER_MODULES`), not a `Notifier` base class or plugin registry - two
concrete implementations (`notify/discord.py`, `notify/pushover.py`) behind
a lookup, nothing more. The two modules share only their pure rendering
rule (a missing or `None` spec field renders as `"unknown"`, never raises) -
pulled into `notify/_shared.py` once a second notifier needed the identical
rule, not as a speculative abstraction.

**Every configured notifier is attempted independently per survivor**, each
wrapped in its own `try/except Exception`, and one `alerts` row is written
per (survivor, notifier) pair - same `sent_at`, same score fields, differing
`notifier`/`delivery_status`. A Discord outage must not cost the user the
Pushover alert they'd otherwise have gotten, and vice versa; sabotage-
verified by removing the per-notifier `try/except` and confirming both
directions (`discord` raising, `pushover` raising) go red exactly as
expected - the surviving notifier stops being attempted and/or the
exception propagates out of `run_alert_cycle` uncaught. All of a survivor's
rows are written in a single `BEGIN IMMEDIATE`/`COMMIT` transaction, after
every notifier for that survivor has been attempted - a crash mid-send must
not leave the item with rows for some notifiers and not others, since dedup
(next paragraph) reads across notifiers and a partial write would leave
that state inconsistent with what was actually sent.

**`alerts.notifiers: []` is valid** and sends nothing, but still writes one
row per survivor (`notifier='none'`, `delivery_status='skipped'`) - the
same "a row must exist for the cooldown/re-alert gate to dedup against"
reasoning V0.9's dry-run rows already established (see this file's earlier
dry-run-as-first-run-guard entry). Sabotage-verified: removing that
branch's row write (falling through to writing nothing when `notifiers` is
empty) turns the "empty list still writes a row" test red.

**Dedup must stay notifier-agnostic - this is the part that actually
matters.** `last_alert()` (`storage/sqlite.py`) returns the most recent
alert row for an item regardless of `notifier`, unchanged in shape from
V0.9, just re-justified: if it filtered by notifier, a Pushover outage
recovering mid-cycle would find no *prior Pushover-tagged* row for an item
Discord had already alerted on, and re-fire on the very next cycle even
though the user already received the Discord alert - dedup has to mean "was
this item alerted on recently, at all," not "was it alerted on recently via
this specific channel." Sabotage-verified against the real function (not a
mock): a row tagged `notifier='pushover'` becomes invisible to
`last_alert()` once a hardcoded `AND notifier = 'discord'` is added to its
query - the failure mode a naive "just check Discord's history" edit would
introduce, since Discord was the only notifier before this milestone. The
first attempt at this test tagged its row `'discord'` to match the only
notifier in play at the time, which passed even against the sabotaged
query by coincidence and proved nothing - the same false-confidence shape
this file's V0.9 cooldown-boundary sabotage hit already (§11, `<` vs. `<=`
at an exact boundary). Tagging the row with a *different* notifier than the
one being sabotaged toward is what actually exercises the invariant.

**Migration 7** adds `alerts.notifier TEXT NOT NULL DEFAULT 'discord'` -
the default both satisfies SQLite's requirement that a NOT NULL column
added via `ALTER TABLE` have one, and correctly backfills every pre-V0.9a
row, since Discord was the only notifier that ever produced one. It also
replaces `idx_alerts_item_sent_at` with an index over
`(item_id, sent_at DESC, id DESC)` - before this milestone, one alert event
produced exactly one row, so a `sent_at` tie within an `item_id` didn't
happen; multi-notifier fan-out makes that the common case (one alert event,
N rows, identical `sent_at`), so the index now matches `last_alert()`'s own
`ORDER BY` exactly instead of leaving the tie-break to an unindexed sort.

**Scripts are now baked into the image** (`Dockerfile`'s `COPY scripts
./scripts`) instead of `docker cp`'d in by hand after every deploy - see
CLAUDE.md's Operational notes for why the manual step existed (and is now
obsolete for anything that isn't a same-session edit to a script file
without a redeploy).

### V0.9b — the `?`-bucket alert gate, and honest lifespan NULLs (2026-09-09)

Two unrelated fixes, motivated by the same class of live-data finding:
something the system does silently was quietly wrong, and both fixes are
about making the system say "I don't know" instead of a plausible-looking
wrong answer.

**Part 1 - `evaluate()` gate 4: do not alert on an incomplete bucket key.**
Live data: 6 of 17 real alerts (35%) had a `?` somewhere in their
`bucket_key` (`1|?|16`, `1|?|?`, `4|?|16`) - the normalize engine's own
marker for "could not extract this field." `engine/baselines.py`'s
`derive_candidates()` already refuses to let a `?`-bearing bucket key
contribute a baseline *candidate* (its `no_question_mark` filter, V0.8a) -
a bucket built from partial specs would poison the very percentiles every
OTHER listing in that bucket gets scored against. Alerting was not holding
that same line: a listing scored against a baseline can never have been
resolved more precisely than the bucket key it's scored under, so scoring
one with a `?` in it and calling the result "a deal" is the same category
of mistake `baselines.py` already guards against, just on the read side
instead of the write side. New gate (`alerts.require_complete_bucket`,
default `true`) checks this immediately after the existing variation
exclusion and before `score_listing` is ever called - there is no reason
to resolve a baseline or compute a ratio for a listing that can't alert
regardless of the result. This is an alert-path gate only: `?`-bucket
listings are still sighted, still get observations, still count toward
everything else. The gate is generic by construction (`?` is the engine's
unknown marker across every profile), so no profile-specific knowledge
enters `engine/alerting.py`.

Inserting this gate renumbers the gates after it (4 through 8a/8b in
`evaluate()`'s comments and this file's own historical descriptions of
them are now one higher each, except cooldown/re-alert which was already
labeled 8a/8b and needed no shift) - `tests/test_alerting.py`'s
`test_gate4`/`test_gate5`/`test_gate6` names were updated to match, so a
test's number is always the gate's actual position in the chain, not a
label that drifted out of sync with an inserted check.

**Part 2 - `lifespan_mins` is `NULL`, not `0`, when a listing was never
sweep-confirmed.** `storage/sqlite.py`'s death-marking `UPDATE` (inside
`record_sweep`) computed `lifespan_mins = (last_seen - first_seen) / 60`
unconditionally. `first_seen` is set once, at insert; `last_seen` only
advances when a **sweep** confirms the listing still present. A listing
inserted by the 5-minute poll and never confirmed by any sweep before
dying keeps `last_seen == first_seen`, so this wrote `lifespan_mins = 0` -
indistinguishable from "sold in under a minute," which is precisely the
band the survival baseline weighs most heavily. Live count: 61 of 694
non-null lifespans (8.8%) are exactly 0, and zero are negative - consistent
with this mechanism, not a clock race. `engine/baselines.py`'s
`derive_candidates()` was never fooled by this - its own
`first_seen == last_seen` check (V0.8b) already excludes these rows from
baseline candidacy, and that filter is unaffected by this change; it must
keep working standalone and must not start depending on
`lifespan_mins IS NULL` instead. This fix is for every OTHER reader of the
column that had no equivalent filter: ad-hoc queries, Datasette, the V1.0
MCP server - anyone reading `lifespan_mins` directly and trusting a `0` to
mean what it says.

The fix: `lifespan_mins = CASE WHEN last_seen > first_seen THEN
(last_seen - first_seen) / 60 ELSE NULL END`. `gone_at` is unchanged
either way - the listing IS dead, only its lifespan is unknown. A listing
genuinely swept at least once and found dead within the same minute also
produces `lifespan_mins = 0`, and that IS a real measurement (`last_seen >
first_seen`) that must survive untouched - the `CASE` distinguishes the two
0-producing shapes by the same `first_seen`/`last_seen` comparison
`baselines.py` already uses, not by the value 0 itself.
`scripts/backfill_zero_lifespan.py` (new, same dry-run/idempotent shape as
`scripts/repair_false_gone.py`) nulls existing rows matching
`lifespan_mins = 0 AND last_seen = first_seen` - the second condition is
load-bearing: a blanket "null every 0" would destroy the genuine
fast-sale-within-a-sweep measurements alongside the fabricated ones.

Both changes were sabotage-verified against the real functions: removing
gate 4 turns the `?|?|?` test red (with a downstream `AttributeError` from
`score_listing` returning `None` inside a mocked-test context, evidence
the gate's absence really does let the listing reach scoring); dropping
the `CASE` back to the unconditional expression turns the never-swept
lifespan test red (`0` where `None` was expected).

Also decided against, per the milestone's out-of-scope list: no Gen 3/4
soldered-RAM suppression rule (§5.7's RAM table below informs whoever picks
that up next, but alert volume in that class doesn't currently justify the
work); no changes to `accessory` or any other reject rule; no changes to
scoring or baseline math; no new notifier work; no attempt to raise sweep
coverage or the miss threshold further (the 8.8% figure here is a known
consequence of hourly sweeps at ~93% coverage, not a new problem to chase
in this milestone).

Renumbered from V0.9b to V0.9c (2026-09-09): this milestone (the `?`-bucket
gate and lifespan NULLs) claimed V0.9b, colliding with the soldered-RAM
labeling milestone that was itself moved here from V0.9a on 2026-09-08 -
see §5.7's amended heading and this file's V0.9a entry above. Same
reasoning as that earlier rename: whichever milestone is actually being
worked on next keeps the next free letter; a not-yet-built one gets pushed,
not the other way around.

## 12. V0.10 — Status module and CLI health script (rough draft, 2026-09-09)

Not yet built. Recorded now, ahead of V0.9c finishing, so the design lands
in one place before implementation starts rather than being reconstructed
from a conversation later.

### The problem

There is currently no positive signal that collection is working. Every
`logger` call in `engine/collector.py` is a warning, an info-on-skip, or an
exception - a healthy sweep logs nothing, so quiet logs are
indistinguishable from a dead loop. `/health` reports budget only.
Answering "did a sweep run in the last hour" today means hand-writing SQL
against `listings.last_seen` on the host.

### The decision that matters

**One `collect_status()`, several renderers.** The status query set is
wanted by at least four consumers: a CLI script, `/health` or `/status`,
the V0.11 dashboard, and the V1.0 MCP `status()` tool. Implemented
per-consumer, they will drift, and "sweeps today" will quietly mean four
different things. This is the same drift class as the collector and
`backfill_normalize.py` diverging on `normalize_input_fields()` (CLAUDE.md,
"Two triggers, one path"), which cost a week of silently unrepaired rows.

    dealwatch/reporting/status.py   collect_status(conn) -> dict
    scripts/status.py               prints it
    main.py                         serves it
    (V0.11)                         renders it
    (V1.0)                          exposes it as an MCP tool

`collect_status()` takes a connection and returns a plain dict. No
printing, no formatting, no FastAPI import, no I/O of its own. Every other
consumer is a thin shell.

### Rough shape of the payload

Grouped by the question each group answers. Exact fields are milestone work.

- **Is it alive** - age of last sweep and last observation, sweeps and poll
  cycles today, budget used/remaining/ceiling.
- **Is it collecting cleanly** - active count, new listings today, deaths
  today, deaths today that were never swept (the unresolved ~8.8%, §11's
  V0.9b dated entry), `spec_status` breakdown, `pending`/`stale` counts,
  last sweep's coverage percentage.
- **Is it finding anything** - alerts today split dry-run vs real, alerts
  over 7 days, distinct items alerted, best ratio in 24h.
- **Is the baseline maturing** - buckets in `baselines`, buckets at or
  above `min_samples`, dead `spec_status='ok'` count, share of scored
  listings that fell back to a seed rather than a derived baseline.

The last group is the only readout on whether the survival baseline is
converging or stalled, and is the reason this milestone is worth its own
slot rather than being folded into V0.11.

`sweeps` (V0.8d) is what makes "sweeps today" honest. Counting distinct
`listings.last_seen` values approximates it and breaks the moment two
sweeps land in the same second.

### Live verification

Mocks prove the SQL shape. They cannot prove the numbers are true. Verify
against the live LXC database by cross-checking at least two fields
against hand-written SQL, and by confirming the sweep-age field moves
after a real sweep lands.

## 13. V0.11 — LAN dashboard (rough draft, 2026-09-09)

Not yet built - depends on V0.10's `collect_status()` existing first (§12).
Recorded now for the same reason as §12.

### Scope

One server-rendered HTML page over `collect_status()` plus a small number
of bounded list queries. Jinja2, no build step, no framework, no npm.
`<meta http-equiv="refresh">` for auto-update.

### Decisions

**Read-only, permanently.** Nothing in the UI mutates data, triggers a
sweep, fires an alert, or edits a profile. Same posture as the MCP server
(§8): a query interface over data the collector already gathered, not a
control plane. A dashboard that can act is a dashboard that can act by
accident.

**No new trust boundary.** The app is already published on 0.0.0.0:8087
with no authentication and the LXC is not port-forwarded (§3.1). A page on
the same app adds nothing to the exposure surface and therefore needs no
auth of its own. This holds only as long as the LAN-only property does.

**Vendor static assets; no CDN.** A dashboard that breaks when the WAN link
is down fails at one of the times it is most wanted. Any chart library
ships as a file in `static/`.

**Bound every query.** Existing indexes are `(item_id, observed_at)` and
`(bucket_key, gone_at)`. A "most recent listings" panel ordering by
`first_seen DESC` hits neither and full-scans `listings` on every render,
on a refresh timer, while the collector writes. Either constrain each
panel with an indexed predicate or add the covering index as a migration -
decided in-milestone, but decided before the page exists.

### Rough panel set

Status block as plain numbers; alerts-per-day over ~14 days; last N alerts
with live eBay links; last N listings ingested; baseline coverage as
buckets-at-threshold over total. One screen.

### Live verification

Load it on the LAN from a machine that is not the LXC, with the WAN link
down, and confirm every panel renders and the numbers agree with
`scripts/status.py` run at the same moment.
