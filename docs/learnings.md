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
