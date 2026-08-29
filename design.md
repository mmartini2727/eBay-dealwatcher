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
3. **Survival-derived baseline (week 4+).** The real one. Persist every listing
   seen, record when it stops appearing in results, and derive `lifespan`. A
   listing that vanishes quickly was probably priced to sell. The distribution
   of prices for fast-disappearing listings in a bucket is a better answer to
   "what price gets sniped" than sold comps would be, because that is the
   question we actually care about.

**Known weakness of (3):** disappearance conflates *sold* with *ended early* or
*pulled by seller*. `getItem` on a dead listing errors and does not disclose
which. Mitigate by weighting listings that vanish well before their scheduled
end date. Accept the noise; it is a deal finder, not an appraisal service.

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

listings(item_id PK, watch_id, title, price, ship, total, condition_id,
         seller, buying_option, end_date, spec_json, bucket_key,
         first_seen, last_seen, gone_at, lifespan_mins)

baselines(watch_id, bucket_key, n, p10, p25, p50, computed_at)

alerts(item_id, watch_id, sent_at, price_at_alert)
```

- `PRAGMA journal_mode=WAL` at init. The collector writes while the MCP server
  reads; WAL keeps readers from blocking.
- Dedup on `item_id`. **Re-alert when price drops materially below the price we
  last alerted at** — sellers revise BINs downward and that is frequently the
  actual deal.
- `bucket_key` is the unit of price comparison. For ThinkPads it is at minimum
  `(model, generation, cpu_family)` and preferably includes RAM and storage
  tiers.

### 4.2 The listing history is the irreplaceable asset

Code can be rewritten. Three months of accumulated comps cannot. The SQLite
file lives in the Docker LXC and is therefore in PBS, but SQLite inside a live
LXC backup is not guaranteed consistent — take a periodic `VACUUM INTO` dump to
the NAS as a second copy.

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
  Frequently *not* in the title — check condition ID and the sub-description.
- **Lot listings** — "Lot of 5", "x5", "Bulk". One listing, N machines.
- **Accessories** — docks, palmrests, keyboards, LCD assemblies, motherboards.
  All match a keyword search for "ThinkPad T14".

### 5.2 Attribute extraction

Parse generation, CPU family, RAM, storage, screen from the title into a `Spec`.
Gen 1 and Gen 2 exist in both Intel and AMD variants and they are not
interchangeable (Gen 1 AMD = Ryzen 4000, Gen 2 AMD = Ryzen 5000).

Listings that fail to parse get `spec = unknown`: **excluded from baseline
computation**, but still eligible to be alerted on if the price is low enough to
be interesting regardless.

### 5.3 Sanity floor

Anything below ~25% of its bucket median is presumed junk that slipped the
reject filters. Flag for manual review; do not alert. Every one of these is a
missing reject rule — treat the queue as a to-do list.

### 5.4 Best Offer

`buyingOptions` including BEST_OFFER means the listed price is an anchor, not a
transaction price. Weight accordingly; do not let it pollute baselines.

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
- Budget math: 5 watches × 288 polls/day × ~2 calls ≈ 2,900/day. Comfortable.

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
