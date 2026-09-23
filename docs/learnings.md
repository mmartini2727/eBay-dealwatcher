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
half-applied filter is worse than the documented gap, because it would
look fixed while still being wrong somewhere else in the chain.

**Correction (2026-09-20): the multi-profile model is decided
(design.md §16 P1 - one database, `profile_id`-scoped) - this fix is
unblocked, not waiting on anything further.** The earlier version of
this entry said the fix needed the multi-profile model decided first;
that is no longer true, and leaving that sentence in place is exactly
what would stop someone from doing the fix now that the thing it was
waiting on has happened. The shape of the fix (§16 P6): thread
`profile_id` through `_derive()`, `derive_candidates()`,
`derive_candidate_pool_stats()` (and `derive_candidates_with_stats()`,
V0.13 Part B) and **every** call site: `scripts/recompute_baselines.py`,
`scripts/baseline_report.py`, `reporting/panels.py`'s `baseline_queue()`,
and `mcp_server/server.py`'s `get_market_price()`, which calls
`derive_candidates(conn)` directly in its `no_computed_baseline` branch
(line ~1317) - a fourth caller the first version of this correction
missed, and exactly the half-applied-filter failure this entry's own
text warns about above. `server.py`'s `get_review_queue()` is covered
transitively via `baseline_queue()` - four call sites, not five, and not
three. All of them together, in one change - no schema migration
required, so it can land today against the single real profile. The
required test needs a two-profile fixture whose dead
listings land in the SAME `bucket_key` string (collision across profiles
is possible and must not be assumed away), asserting each profile's
percentiles come out unmixed; a single-profile fixture passes whether or
not the filter is there and proves nothing. Land this before the
baseline recompute is ever put on a schedule - a cron silently
contaminating baselines is worse than a manual one, because nobody is
watching the output closely enough to notice.

**Shipped 2026-09-20 - six call sites, not four.** This correction's own
count was still short by two. A required, pre-edit enumeration pass over
every caller of `_derive`/`derive_candidates`/`derive_candidate_pool_stats`/
`derive_candidates_with_stats` (not a repeat of the same grep that missed
site 4 above, but a fresh one, checked against this entry's list rather
than trusted to match it) found a fifth and sixth site neither this entry
nor design.md §16 P6 had recorded: `scripts/bucket_key_dryrun.py`'s
`verify_against_derive_candidates()` (calls `baselines.derive_candidates()`
directly, to compare against the dry-run's own derivation) and
`derive_pre_bucket_candidates()` in the same file, which runs its own
query, `_DRY_RUN_LISTINGS` - textually `_DEAD_OK_LISTINGS` with the SELECT
column list swapped via `.replace()`, so it silently inherited the new
`profile_id = ?` placeholder the moment the base query changed. See L16
below for what that inheritance mechanism means for the *next* change to
`_DEAD_OK_LISTINGS`, which will not be caught the same way this one was.

All six sites now pass `profile_id` as a required keyword-only argument
with no default, anywhere - not `profile_id: str | None = None`, no
module-level fallback constant, no "all profiles" branch. Comments
claiming the query was unscoped were corrected at every site that stated
it: `reporting/status.py`, `reporting/panels.py`'s `baseline_queue()`,
`engine/baselines.py`'s own `_DEAD_OK_LISTINGS`, and
`mcp_server/server.py`. Required test: a two-profile fixture whose dead
listings collide on the same `bucket_key` string, asserting each
profile's percentiles come out unmixed. Sabotage-verified by a real
edit-run-revert pass (defeat the filter as `profile_id = ? OR 1 = 1` -
keeping the bound parameter so binding doesn't error, which is the
semantically correct sabotage for a `WHERE` clause becoming a no-op, as
opposed to deleting it outright and getting a binding-count error
instead of the intended silent-pooling behavior): only the new
two-profile test went red, all 27 pre-existing `test_baselines.py` calls
(mechanically updated to pass a real `profile_id`, not a default) stayed
green, and the full suite passed at 626 both before sabotage and after
revert - exactly the prediction stated before the sabotage was run, not
adjusted after the fact. Schema unchanged, still version 8.

Live-verified 2026-09-20 against the real LXC, one profile: unfiltered
and filtered candidate counts both 778 - the filter is a confirmed no-op
with one profile today, checked directly against the database rather
than inferred from the sabotage test alone.

**Coverage limit:** the sabotage proves the filter is load-bearing inside
`engine/baselines.py`. It does not prove any of the six call sites passes
the *correct* `profile_id` - a site passing a wrong-but-valid value would
leave this test green, since the test exercises the engine function
directly, not any call site's own argument. Not covered by choice; the
write path (`run_recompute()` against a two-profile fixture) is where a
wrong-value bug would actually surface, and it is not exercised here.

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

**Correction, V0.13:** the 291-second item is very plausibly explained
after all - by a SECOND, distinct mechanism (CLAUDE.md's Open items,
"`last_seen` can predate `first_seen`"), not the first. `record_sweep`
stamps `last_seen = swept_at`, the sweep's START time, and a sweep takes
minutes to run; a listing inserted by a poll while a sweep is in flight,
and captured by that same sweep's seen-set, gets a `last_seen` from
before its own insert - by roughly however long the sweep took, which is
the right order of magnitude for a multi-minute gap, unlike the ~2-second
sweep/death race above. Not proven - no direct evidence ties this
specific item to a sweep that was actually in flight at the moment of its
insert - but the original "gap too large to be the poll/sweep race"
reasoning assumed only one mechanism existed, and it no longer does.

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

**Correction (2026-09-20 live report): the violation reversed as n grew.**
Same bucket, re-measured: fast n=16 $455.00, slow n=9 $474.99 - fast is
now $19.99 *cheaper*, the direction the premise predicts. The original
observation above is left in place rather than edited or deleted, per
this project's own convention for `learnings.md` - the fact that a
small-n violation resolved as the sample grew is itself the learning,
not a reason to erase the record of the violation ever having been seen.
This is one bucket, once; see the broader fast-sale-premise open item in
CLAUDE.md (updated the same date) for how it reads across all eligible
buckets, not just this one.

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

A sabotage value must be chosen to diverge from the fixture's expected
result, not merely to differ from correct code. Hardcoding "seed" as a
sabotage passed because the fixture's true answer was also "seed" — the
check ran, went green, and proved nothing. Same class as a DST fixture
anchored on the transition day. Verify the sabotage goes red before
trusting that it can.

## L9 — a compound class selector silently never matches the legend swatch

Found while wiring V0.12b's own legend: `.bar-segment.bar-live` and
`.bar-segment.bar-dry` require an element to carry BOTH classes. The real
bar segments do (`class="bar-segment bar-live"`), but the legend swatches
never did (`class="legend-swatch bar-live"`, no `.bar-segment`) - so the
Live/Dry-run legend has shown no colour at all since the alerts-per-day
chart was built. `.legend-swatch` itself sets `display: inline-block`
with an explicit `width`/`height`, so this is NOT L7's bug (inline
elements ignoring block dimensions) - the swatch has always had correct
geometry, just no colour. A different mechanism, same shape of mistake:
a selector written against the assumption that a colour class always
appears alongside a specific structural class, on an element where it
doesn't.

Fixed by unscoping the colour rules (`.bar-live`, `.bar-dry`,
`.ratio-bar-computed`, `.ratio-bar-seed` - no `.bar-segment` prefix), so
one class colours whichever element carries it, structural class or not.
Written correctly from the start for V0.12b's own new legend, then
applied back to the pre-existing bug once noticed.

Same lesson as L7 anyway: a colour/geometry rule that depends on a
second class being present needs that dependency checked against every
element the class is used on, not just the one the rule was written
for - a template's `<span>`/`<div>` choice was L7's version of this,
a legend reusing a bar's class name is this one's.

## L10 - an invariant documented confidently in three modules, held in every test, falsified by ordinary operation

`sweep_bookkeeping_consistent` (`reporting/status.py`) compared the
UNFILTERED `MAX(listings.last_seen)` against `last_sweep_started_at`, on
the premise that only `record_sweep()` ever advances `last_seen`. That
premise was false the whole time: `record_sighting()`'s insert branch
writes `last_seen` once, for every brand-new listing (a new row needs a
value). At the live rate (~31 new listings/day against hourly sweeps),
an ordinary poll discovering a new listing pushed the unfiltered max
ahead of the last sweep stamp close to half the time - the indicator sat
amber on a collector that was completely healthy. Confirmed live: one
newly-inserted listing, `sweep_bookkeeping_consistent = False`, no other
symptom (V0.13, design.md §14).

Every test written against this check passed, because they were all
written from the SAME false premise the check itself encoded - a test
built on a wrong assumption confirms the assumption, it doesn't catch it.
Nothing here was a testing failure; the premise itself was wrong, and no
amount of testing against a wrong premise finds that out.

The false claim had propagated by citation, not by copy-paste:
`docs/design.md` §4.2 stated it first ("Only the sweep writes
`last_seen`"), `reporting/status.py` and `storage/sqlite.py` each cited
§4.2 in their own docstrings, and the dashboard's health check cited
`status.py`. Fixing only the check (`reporting/status.py`) would have
left the claim alive and citable in three other places, ready to be read
and trusted again the next time someone builds something on top of it.
Correcting a wrong invariant means finding every place it was stated,
not just the one place it broke something.

## L11 - two measurement errors in one ten-minute window, diagnosing L10's own log line

While confirming V0.13 Part B's log-volume premise before writing it into
a milestone justification, two independent mistakes turned up in the same
short check:

1. `docker logs dealwatch | grep "negative lifespan"` reported 0 matches
   while the matching lines were visibly scrolling past unfiltered in the
   same terminal. Python's `logging` module writes to stderr by default;
   `docker logs` preserves the stdout/stderr stream split, and a bare pipe
   only carries stdout. The fix is `docker logs dealwatch 2>&1 | grep ...`
   - trivial once seen, and silently wrong (a confident, plausible "0")
   until then.
2. Two different *rate* estimates for the same line - a by-eye read of a
   scrollback burst (~10/min) and a count of what visibly passed through
   the broken, stdout-only pipe above (2 per 10 min) - were both wrong.
   The real, correctly-piped count was 11 per 10 minutes with a dashboard
   tab open.

Neither error would have been caught by rerunning the same check the same
way - both looked like real measurements. A rate claim about a running
system needs a counted number from a command that is itself verified
correct (here: confirm the pipe actually carries what you think it
carries), or it doesn't belong in a milestone's justification section.

## L12 - the dropped-count line was nested inside the branch that disappears exactly when it matters most

V0.13 Part B put `dashboard.html`'s "N candidates dropped: negative
lifespan" line inside `{% elif payload.baseline_queue.queue %}` - the same
branch as the queue list itself. That means the line only ever rendered
alongside a non-empty queue. The moment every bucket reaches a computed
baseline (`queue` becomes `[]`), the template falls to `{% else %}` and
shows only "No buckets waiting on a baseline right now" - silently
dropping the count. That is exactly the state this project is working
toward, and exactly the state where a nonzero drop count is most worth
surfacing: nothing else on the page would tell a maintainer that data is
still being silently excluded.

Caught by review before deploy, not by the test suite - `dashboard.html`
has no test coverage by design (L8), so a fixture with `queue: []` and a
nonzero `negative_lifespan_dropped` was never exercised by anything
automated. Fixed by making the drop-count line a sibling of the
queue-or-empty-message branch, not a child of it, so it renders whenever
`baseline_queue` isn't an error, independent of whether the queue itself
is empty.

Same shape as L7/L9's lesson, one level up: those were rules that assumed
a second class or property always co-occurred with a first; this was a
branch that assumed a value's relevance was conditional on ITS SIBLING's
value, when the two were actually independent facts that happened to be
computed by the same function. Any V0.11a Part A message and any nonzero
count from a different section of the same payload dict should be
checked for this same nesting mistake before being added.

## L13 - an ad-hoc check that doesn't copy the module's own predicate isn't checking the module

Second time in the same session a live/ad-hoc check diverged from what it
was meant to verify without raising any error - L11's `docker logs | grep`
(checking the wrong stream, silently) and, separately, the reasoning that
almost filed `last_seen` predating `first_seen` as impossible before the
V0.13 open item on that exact shape turned it up. The common failure: a
check built from a paraphrase of a module's logic - "these two columns
should be ordered," "the log line went to this stream" - rather than the
module's own predicate, copied character for character.

Concretely here: `_derive()`'s guard is `first_seen != last_seen`, and
V0.9b's fix is `CASE WHEN last_seen > first_seen`. Neither says "assume
last_seen >= first_seen" anywhere, and both happen to handle
`last_seen < first_seen` correctly - but only because `!=` and `>` are
exactly the operators that don't need that assumption, not because
anyone verified it holds. An ad-hoc check written as "confirm
`last_seen >= first_seen` for every row" would have reported a violation
and looked like a new bug, when the real code was already fine and the
paraphrase was the thing that was wrong. `!=` versus `>` on two columns
you assume are ordered is exactly the size of mistake that survives
review - it doesn't fail loudly, it just quietly checks something
adjacent to the real question.

The rule: when a live check exists to verify a module's assumption, its
SQL predicate must be the module's own predicate, not a restatement of
what the predicate is "supposed to mean." If copying it exactly feels
redundant, that redundancy is the point.

## L14 - "connected" is not "called": a confident client narration is not evidence a tool ran

V1.0 prompt 1's live verification (design.md §15, step 5: `claude mcp add`
+ ask Claude Code for system health and to explain a real listing) - the
first attempt produced a confident, detailed, and ENTIRELY UNVERIFIED
answer. It read like a real `explain_listing()` result, but at least two
fields in it do not exist anywhere in this server's actual output shape:
`n=12` (this server's score section calls it `baseline_n`, and nothing
computed 12 for this listing) and "cleared every alert gate" (no tool in
this server evaluates alert gates at all - that's `engine/alerting.py`'s
`evaluate()`, explicitly out of scope for `explain_listing`, per its own
docstring). The client had a dashboard open on port 8087 in the same
session and, plausibly, answered from that instead of actually calling
the tool - nothing about the ANSWER made this obvious; it was fluent,
specific, and wrong in a way that read as right.

The only evidence a tool call actually happened is the SERVER's own log
(or an explicit trace of the JSON-RPC exchange) - never the client's
narration of what it did, no matter how confident or detailed. A
distinguishing detail is not the same as a real observation; "cleared
every alert gate" sounds like something a real tool would say precisely
because it's plausible domain language, not because anything checked it.

Same class of failure as a test that passes for the wrong reason (L8's
sabotage discipline, extended past testing into live use): a check that
LOOKS like it exercised the thing being verified, without actually doing
so. The fix is procedural, not technical - when verifying an MCP tool (or
any tool-calling client) actually ran something, check the callee's own
side, not the caller's summary of it.

## L15 - a tool description is advice a model may drop; anything that selects a code path must be enforced in tool code

V1.0 prompt 2's live pass (design.md §15's 2a addendum): `get_market_price`
called with `generation="Gen 2"`, `cpu_family="Ryzen 5000"`,
`ram_tier="16GB"` - none of which are this profile's real values (`2`,
`amd-ryzen-5000`, `16`) - did not error. The computed-baseline lookup
missed on the malformed `bucket_key` (as it should have - that bucket_key
genuinely has no computed row), execution fell through to the **seed**
layer exactly as it would for any bucket with no computed baseline yet,
and the tool returned a confident, correctly-caveated, WRONG price. The
tool's own description already said generation/cpu_family/ram_tier had to
match normalized fields - that caveat did nothing, because a description
is read (or not) by the model, not enforced by the runtime.

The seed-layer fallthrough is not the bug - it is a legitimate branch,
the correct answer for a real bucket that just hasn't reached
`min_samples` yet. The bug is that the SAME branch was also silently the
destination for a value nobody in this system ever produces. Nothing in
`get_market_price`'s code path could tell "immature but real" apart from
"not real to begin with" - both looked exactly like "no computed baseline
row," because both took the identical code path to get there.

The general shape: a tool DESCRIPTION is advice - the model may summarize
it, forget part of it, or just not have read it closely enough to catch
that "Gen 2" isn't the same string as "2". Tool CODE is a contract - it
runs on every call regardless of whether the description was read at all.
Any caveat that changes which code path executes (not just how a result
is interpreted) has to be enforced where the branch actually happens, not
stated next to it and trusted. `dealwatch/mcp_server/vocabulary.py`'s
fix - reject an out-of-vocabulary `generation`/`cpu_family`/`ram_tier`
as a structured error before any lookup runs, case-insensitive exact
match only, no fuzzy correction - is the general pattern: a legitimate
fallback branch must never also be able to be reached by malformed input
that was never a real case of the thing the fallback exists for.

**Extension (V1.0 prompt 2b, design.md §15's dated 2b addendum): the fix
above created two smaller versions of the same underlying mistake.**

First - **a vocabulary derived from observed data goes stale as new data
arrives; caching it for a process lifetime turns a new, valid
configuration into a false rejection.** `cpu_family`'s vocabulary was
correct at the moment it was built (2a's own fix), but it was built ONCE,
at import, and never touched again. The first real, freshly-collected
listing in a `cpu_family` this server had never seen before (a new CPU
generation entering the market, say) would have been rejected as
"unknown" - a legitimate, collected fact about the marketplace, refused
by the exact mechanism built to stop refusing legitimate facts, for the
same underlying reason: code that was supposed to track a source of
truth instead had a hardcoded (if honestly-labeled) snapshot of it. The
fix is a short TTL (60s) on the observed half specifically - the
profile-sourced half never needs one, because nothing about `tiers`/
`derive` changes without a profile edit, which already needs a restart.

Second, and easy to miss because it looks like the opposite problem: **a
fail-closed degradation that nothing reports is indistinguishable from
working.** `cpu_family`'s vocabulary going empty (a database unreadable
at container boot) rejected every value with a perfectly well-formed,
correctly-shaped error - the SAME response shape a real bad value gets.
`/health` still said `200 ok`. Nothing anywhere said "this field's
vocabulary is currently empty and every call is about to fail" - the
degradation was total for that field and silent for the whole system.
Being narrowly correct in each individual rejection didn't make the
aggregate state visible; only reporting `source`, `count`, and
`built_at` per field (now in `/health`) does that.

Both fixes are cheap; neither was in the original 2a scope, because 2a
was about REJECTING bad input correctly, and both of these are about
what happens to a CORRECT rejection mechanism over TIME - a dimension
input validation alone doesn't have anything to say about. A validation
fix is not finished just because it rejects the right things at the
moment it's written; it also needs a story for what it does an hour
later, and for what a caller (or an operator) can see when that story
goes wrong.

## L16. `_DRY_RUN_LISTINGS`'s `.replace()` coupling to `_DEAD_OK_LISTINGS` - inheritance that only fails loudly by accident

`scripts/bucket_key_dryrun.py` builds `_DRY_RUN_LISTINGS` as
`_DEAD_OK_LISTINGS` (`engine/baselines.py`) with its SELECT column list
swapped via a Python `.replace()` call - the WHERE clause is inherited
verbatim, unread, every time either string changes. When L2's fix added
`profile_id = ?` to `_DEAD_OK_LISTINGS`'s WHERE clause, `_DRY_RUN_LISTINGS`
picked it up automatically, and that was caught safely only because the
change it needed to survive happened to raise
`sqlite3.ProgrammingError: Incorrect number of bindings supplied` the
instant `derive_pre_bucket_candidates()` ran unmodified against the new
query text - a loud, immediate, unmissable failure.

That is not a property of the coupling; it is a property of this
particular change happening to alter the bound-parameter count. The
learning is the inverse case, and it is the actual point of this entry: a
future edit to `_DEAD_OK_LISTINGS` that adds a WHERE condition needing
**no** new parameter - a literal comparison, another `IS NULL`, a fixed
date bound - propagates into `_DRY_RUN_LISTINGS` exactly as silently as
the profile_id clause did, except nothing errors. The dry-run's self-check
(`verify_against_derive_candidates()`, comparing its own derivation
against `baselines.derive_candidates()`) would keep passing, having
silently started comparing two queries whose relationship changed, with
no edit made in this file and no test that would notice. Inheritance via
`.replace()` is the intent right up until the day it isn't, and nothing at
the `bucket_key_dryrun.py` end marks where that boundary is. Not fixed
here - the L2 task that surfaced this was explicitly scoped not to
consolidate or redesign `bucket_key_dryrun.py`'s own copy of this query
(or its separate `open_readonly()` copy); recorded so the next person
editing `_DEAD_OK_LISTINGS` checks this file by hand rather than trusting
the test suite to catch a parameter-count-preserving change.

## L17. DELETE-then-INSERT means a zero-bucket recompute wipes baselines, and the staleness indicator stays quiet

`engine/baselines.py`'s `store_baselines()` deletes every existing
`baselines` row for a profile, then inserts the newly-derived rows, in one
transaction - the natural shape for "replace the profile's baselines with
what was just computed," and correct for every ordinary run. It has one
consequence nobody had reason to think through until scheduling made a
bad run possible without a human watching: a recompute that derives *zero*
qualifying buckets (a bad profile edit, `min_samples` misconfigured, a
transient database problem) doesn't leave the profile with stale rows -
it leaves it with **zero** rows. Scoring falls through to the seed layer
for every bucket, alerts on computed-baseline buckets stop meaning what
they used to mean, and `baselines_age` (`BASELINE_STALE_WARN_MINS`,
`reporting/indicators.py`) cannot flag any of it as amber, because there
is nothing left in the table to be old - the indicator watches an age, not
a row count.

`scripts/recompute-baselines.sh` guards this by counting `baselines` rows
for the profile before and after each run and failing the whole
invocation if a non-empty count became zero. **This is a detector, not a
lock** - the guard fires after the DELETE has already committed, which is
only an acceptable shape because the recompute is cheap, idempotent, and
a snapshot exists from fifteen minutes earlier (design.md §17, Choice 3).
A system where the wipe itself were expensive or irreversible would need
the check before the transaction, not after it.

Sabotage-verified on the real LXC: a copy of the live profile with
`min_samples: 9999` (making every bucket fail the sample-count gate)
recomputed to zero qualifying buckets, the guard fired, and the wrapper
exited 1.

## L18. Two profile YAMLs sharing an `id:` silently clobber each other's baselines

Found by accident, sabotage-testing L17's guard: a copied
`thinkpad-t14.yaml`, edited for a hypothetical second profile but never
given its own `id:`, ran second in the cron wrapper's loop and overwrote
the first profile's baseline rows outright - `store_baselines()` has no
way to know two files claimed the same identity, it only sees the
`profile_id` it was handed. L17's zero-wipe guard does **not** catch this
case, because the row count after the second run is non-zero - it looks
like a completely ordinary successful recompute, for the wrong profile's
data.

This is the copy-paste path to an actual second profile: duplicate an
existing YAML, edit the search terms and thresholds, and forget to change
`id:`. Nothing rejects the file at load time, nothing rejects it in the
collector, and this script would have run it nightly, clobbering the
same profile's baselines every night, until someone noticed the
percentiles looked wrong and had no obvious reason why.

Now guarded structurally rather than by inference: the wrapper collects
every profile's `id:` up front, before the recompute loop starts, and
refuses the entire run on any duplicate - `FAIL duplicate profile id(s):
...`, exit 1, nothing executed. This is the same identity-collision shape
as design.md §16 P6's `listings` primary-key problem (`item_id` alone,
not `(profile_id, item_id)`) - a value that is supposed to be unique
across profiles with nothing in the schema or the loading path actually
enforcing it. §16 P6 now cross-references this entry.

## L19. An unquoted `&` in an environment variable set on the command line never reaches the script, and it reports success anyway

Running `DEALWATCH_KUMA_PUSH_URL=https://kuma.example/api/push/TOKEN?status=up&msg=OK
./scripts/recompute-baselines.sh` looked like it set the variable and ran
the script. It did neither, correctly. Unquoted, the shell treats `&` as a
job-control operator - it split the line at `&`, so `msg=OK
./scripts/recompute-baselines.sh` ran as its own (immediately-failing)
background job, and `DEALWATCH_KUMA_PUSH_URL` was set only in the
environment of the job that exited before doing anything. The actual
script invocation ran with the variable unset.

The wrapper's push block is conditional on the variable being non-empty,
so an unset variable doesn't error - it just skips the block silently.
The script therefore ran the real recompute, printed `ok`, and exited 0,
having sent nothing to Kuma. The visible symptom was not a failure at
all: a monitor that never changes state, which reads identically to a
monitor that is passing, right up until someone needs the heartbeat and
finds it stopped an unknown number of days ago.

Fixed at the call site, not in the script: single-quote the URL, and pass
only the base push endpoint with no query string. The script already
builds the query itself (`curl -G --data-urlencode "status=..."
--data-urlencode "msg=..."`) - a URL that arrives with its own `?status=`
already attached produces a malformed double-query request even once the
quoting is fixed, so both parts of the call site matter, not just the
quoting. See README.md's command examples, which use the corrected form.