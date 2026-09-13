# learnings.md — things found in a live session, recorded once

Not a design doc and not a spec. Each entry below came out of comparing
live numbers against each other during V0.11/V0.11a/V0.11b dashboard work
and would otherwise exist nowhere but a chat transcript. design.md is
still authoritative for anything these entries reference.

## L1. Two different "dead spec-ok listing" counts, both correct

`reporting/status.py`'s `dead_spec_ok_count` (surfaced on the dashboard as
of V0.11b) and `engine/baselines.py`'s `_DEAD_OK_LISTINGS` (the query
behind `derive_candidates()`, and therefore behind
`baseline_queue()`/`computed_baselines()` and
`scripts/baseline_report.py`'s own counts) answer different questions and
will never agree:

- `dead_spec_ok_count`: every row where `gone_at IS NOT NULL AND
  spec_status = 'ok'`. No `variation_id` filter.
- `_DEAD_OK_LISTINGS`: the same, plus `AND variation_id IS NULL` - the
  V0.8d exclusion. A row for one variation of a multi-variation listing
  flaps in and out of search results independent of the listing actually
  dying (10 of 14 resurrection-log warnings traced back to just three
  parent listings), so it's excluded from anything that feeds a baseline.

Live numbers at the time this was written: `dead_spec_ok_count` = 484,
`_DEAD_OK_LISTINGS`-scoped count = 478. The 6-row gap is exactly the
variation rows. Neither number is wrong - checking which one you're
looking at should take ten seconds, not a re-derivation. The dashboard's
"Dead spec-ok listings" indicator is now labeled "(incl. variations)" for
exactly this reason (V0.11b Part E).

## L2. `_DEAD_OK_LISTINGS` has no `profile_id` filter

`engine/baselines.py`'s `_DEAD_OK_LISTINGS` query (and therefore
`derive_candidates()`, `derive_candidate_pool_stats()`,
`baseline_queue()`, and `scripts/recompute_baselines.py`) pools candidates
across every profile in the database, not just the one being asked about.
Harmless today - there is exactly one profile (`thinkpad-t14`) - and
`reporting/status.py`'s own `dead_spec_ok_count` comment already flags
this same gap for a different reader.

**This is a multi-profile prerequisite, not a nice-to-have.** The day a
second profile exists, `derive_candidates()` will pool both profiles'
dead listings together, and `scripts/recompute_baselines.py` will write
the resulting cross-contaminated baselines under a single `profile_id` -
silently wrong, not a crash. Do not add a partial `profile_id` filter to
just one of these call sites without fixing all of them together; a
half-applied filter (query filtered but nothing decided about how
multi-profile baselines should even be scoped) is worse than the
documented gap, because it would look fixed while still being wrong
somewhere else in the chain. Needs the multi-profile model decided first.

## L3. Negative-lifespan anomaly: three items, one explained pattern

Three listings where `gone_at` precedes their last observation's
`observed_at` - `_derive()`'s existing negative-lifespan guard correctly
drops all three (see engine/baselines.py; logged at DEBUG with an INFO
aggregate as of V0.11b Part B1, previously WARNING per item).

- `v1|307170897447|0` and `v1|318849224584|0`: both off by exactly 2
  seconds, sharing the same timestamp - consistent with a sweep/death
  race (a fast poll writes a new observation without advancing
  `last_seen`; if death is detected before the next sweep re-confirms the
  listing, `gone_at` ends up set from a `last_seen` that predates the
  poll's own observation). This is the same root cause as the existing
  "`gone_at` can precede a listing's final observation" open item in
  CLAUDE.md - not a new bug, a second data point for an already-known one.
- `v1|137691325764|0`: off by 291 seconds. Not explained by the
  sweep/death race above - that shape predicts a gap of at most one poll
  interval, not five minutes. Left as an open, unexplained anomaly.

Recorded as an anomaly, not an open bug to fix - the guard already
handles it correctly by dropping the candidate rather than corrupting a
baseline with a negative number.

## L4. First observed violation of the fast-sale survival premise

`3|amd-ryzen-6000|16`: fast median (lifespan < `fast_lifespan_hours`)
$455.00 (n=13) against slow median $400.00 (n=6). design.md §2.1's
survival premise holds that fast-selling listings are the *cheap* ones -
here the fast-selling listings are $55 *more expensive* than the slow
ones. One violation among the buckets with enough data on both sides to
check (`scripts/baseline_report.py`'s section (d) is exactly this check),
and plausibly noise at n=13/6.

**Why this one is consequential and not just an interesting data point:**
this bucket has already crossed `min_samples` and has a *computed*
baseline actively driving live alerts, with nothing on the dashboard
signaling that its own premise check failed. `baseline_report.py`
computes this exact comparison, but nothing runs it on a schedule -
finding a premise violation currently requires a maintainer to manually
run that script and read section (d).

Deliberately not acted on: no scoring change, no filtering, no
auto-exclusion of this bucket. One violation at low n is not evidence the
premise is wrong for this bucket, only evidence worth watching as the
sample grows. If this bucket (or others) keeps violating the premise as
`n` increases, that's the point at which it stops being a footnote.

## L5. `baseline_queue()`'s seed lookup picks ONE listing per bucket - sound only because no seed matches outside the bucket_key today

V0.12 Part B resolves each queued bucket's seed value by reading ONE
representative candidate's `spec_json` and calling `resolve_seed_baseline()`
on it (`reporting/panels.py`). Which listing gets picked is now
deterministic (`min()` by `item_id`, fixed after this shipped without it -
`_DEAD_OK_LISTINGS` has no `ORDER BY`, so "first row `derive_candidates()`
returns" was an implementation artifact of SQLite's default scan order,
not a guarantee, and would have let an unrelated schema/index change
silently flip which listing's spec drove the displayed seed).

Determinism alone does not make picking one listing correct, though - it
only makes the panel's answer consistent from one refresh to the next.
The actual invariant that makes "any one listing in the bucket" a valid
substitute for "every listing in the bucket" is this: **every
`seed_baselines` match key in use today (`generation`, `cpu_family`) is
already a component of `bucket_key`** (`[generation, cpu_family,
ram_tier]` since V0.8c). Every listing sharing a `bucket_key` therefore
necessarily shares the same `generation`/`cpu_family`, so every one of
them resolves to the identical seed - which one gets read is irrelevant
to the *answer*, only to whether the answer is reproducible.

**This breaks the moment a `seed_baselines` match block keys on any spec
field outside `bucket_key`** - `condition`, `screen`, `storage`, anything
not in `[generation, cpu_family, ram_tier]`. At that point two listings
sharing a `bucket_key` could legitimately resolve to two *different*
seeds, and "read one representative listing" stops being a shortcut and
starts being a silent wrong answer for whichever listings didn't get
picked - deterministic, plausible-looking, and quietly disagreeing with
what `score_listing()` would actually compute for those other listings'
real alerts. The comment at `baseline_queue()`'s call site says this
explicitly. Before adding a `seed_baselines` match key outside
`bucket_key`'s three fields, this function needs to change - at minimum,
resolve per-candidate and either show a range or flag the bucket as
seed-ambiguous, not silently keep reading one row.

## L6 — min(item_id) is deterministic per snapshot, not stable over time

baseline_queue() resolves a bucket's seed from one listing's spec, chosen
by min(item_id) over the bucket's fast candidates. That is deterministic
for a given database state but not stable across time: a new dead
candidate with a lexicographically lower eBay id changes which row is
picked.

Harmless while L5's invariant holds, since every listing in a bucket
resolves to the same seed. If the invariant ever breaks, the symptom is
a seed value that changes on its own with no config change. Read that as
"the invariant broke," not "the panel is flapping."

## L7 — height and width are silently ignored on inline elements

The dashboard's pacing bars never rendered from V0.11a through V0.12.
.pacing-track and .pacing-fill were styled with height: 10px and
height: 100%, but the template used <span>, which is display: inline by
default. Height and width do not apply to non-replaced inline elements.
The browser accepted the declarations and dropped them — no console
error, no visual cue that anything was ignored.

Separately, .bar-segment had colour rules under .bar-segment.bar-live
and .bar-segment.bar-dry but no base rule, so the chart's segments had
zero width despite correct inline heights.

Fixed with display: block on the pacing classes and
width: 100%; flex-shrink: 0 on .bar-segment. flex-shrink: 0 matters:
column flex children shrink on the main axis by default, so two
segments summing to 100% can still be squeezed.

The payload, the computed percentages, and the emitted markup were all
correct throughout. Only rendered geometry was wrong.

## L8 — dashboard milestones need a reported visual check

Two V0.11/V0.12 defects were invisible to the full test suite and could
only be caught by loading the page:

  1. A fresh container with no database 500'd, because connect_readonly()
     raised outside build_payload()'s per-section isolation.
  2. Every bar on the page rendered empty (L7).

The bars survived an entire milestone because V0.11a's live-verification
list included "budget bar shorter than the day bar" and that step was
never run or reported.

Payload verification is not visual verification. Every dashboard
milestone gets an explicit browser step whose result is reported back,
not assumed.