"""Bucket-field vocabulary for input validation (V1.0 prompt 2a, design.md
§15's dated addendum). A tool DESCRIPTION is advice a model may drop; this
module is the enforcement point that runs regardless of whether the model
read the description at all. `get_market_price`/`query_listings` reject a
`generation`/`cpu_family`/`ram_tier` value outside its field's real
vocabulary as data, rather than letting it silently fall through to the
seed-baseline layer (a legitimate branch for a bucket with no computed
baseline - it must never also be the branch a typo lands in) or to a
query that matches zero rows with no indication why.

Vocabulary source, per field, tried in this order:

  1. `tiers` - `profile.tiers[field].breaks`' labels. Exhaustive by
     construction: `_apply_tier()` (normalize/engine.py) always returns
     either one of these labels or None, never anything else. Always
     literal, so this is the highest-confidence source when it applies.
  2. `derive` - every `DeriveRule` targeting this field, if EVERY one has
     a literal `value` (no `{N}` capture-group placeholder). A derive
     rule is a finite, profile-authored mapping - inherently enumerable
     regardless of what triggers it, the same way a tier's breaks are.
  3. `extract` - every `ExtractRule` for `profile.extract[field]`, if
     EVERY one has a literal `value`. Some fields' extract rules are
     literal per pattern (a fixed marketplace vocabulary term, e.g.
     `amd-ryzen-5000`); others substitute a regex capture group into the
     value (`'intel-{1}th'`) and cannot be enumerated by reading the rule
     list alone without parsing the pattern - not attempted here, by
     design (see module docstring's "do not extend it").
  4. `observed` - none of the above yielded an all-literal rule set for
     this field. Falls back to the distinct non-NULL values this field
     has actually taken in `listings.spec_json` for this profile - real
     marketplace vocabulary, which cannot drift from what the profile
     actually produces the way a hand-maintained list could.

For `profiles/thinkpad-t14.yaml` specifically: `ram_tier` resolves via
tiers (labels "8"/"16"/"32"/"48"); `generation` resolves via derive (its
own `extract` rules are template-valued - `'{1}'` - but every `derive`
rule targeting `generation` maps a `cpu_family` value to a literal digit
string, and that set is what this module uses); `cpu_family` falls back
to `observed` (its `extract` rules mix literal values with two
capture-group templates - `'intel-{1}th'` - so the rule list alone is not
a safe, complete enumeration; see the real listing data instead).
"""

import sqlite3

from dealwatch.engine.scoring import parse_spec_json
from dealwatch.normalize.schema import Profile

# The three bucket_key fields get_market_price/query_listings actually
# take as arguments - hardcoded to match those tools' fixed Python
# signatures (D13: one profile, not a multi-profile-flexible design), not
# derived from profile.bucket_key at runtime.
VOCABULARY_FIELDS = ("generation", "cpu_family", "ram_tier")


def _literal_or_none(values: list) -> list[str] | None:
    """Sorted distinct string values if every one is literal (not a
    string containing a `{N}` capture-group placeholder); None if the
    list is empty or any value is a template - "not safely enumerable
    from this rule set," not "enumerable but empty"."""
    if not values:
        return None
    literals = []
    for value in values:
        if isinstance(value, str) and "{" in value:
            return None
        literals.append(str(value))
    return sorted(set(literals))


def _tier_vocabulary(profile: Profile, field: str) -> list[str] | None:
    tier = profile.tiers.get(field)
    if tier is None:
        return None
    return sorted({brk.label for brk in tier.breaks})


def _derive_vocabulary(profile: Profile, field: str) -> list[str] | None:
    return _literal_or_none([rule.value for rule in profile.derive if rule.field == field])


def _extract_vocabulary(profile: Profile, field: str) -> list[str] | None:
    extract_field = profile.extract.get(field)
    if extract_field is None:
        return None
    return _literal_or_none([rule.value for rule in extract_field.rules])


def resolve_profile_vocabulary(profile: Profile) -> tuple[dict[str, dict], list[str]]:
    """({field: {"values": [...], "source": "profile"}}, [fields needing
    the observed fallback]) - the profile-only half of vocabulary
    resolution, needing no database access at all (V1.0 prompt 2b,
    design.md §15's dated addendum). Split out from the old single
    build_bucket_vocabulary() so a caller can compute this ONCE, at
    process start, and never touch it again - unlike the observed
    fallback, nothing about a field resolved here can change without a
    profile edit, which already requires a container restart (D13).
    """
    profile_vocabulary: dict[str, dict] = {}
    observed_fields: list[str] = []

    for field in VOCABULARY_FIELDS:
        values = _tier_vocabulary(profile, field)
        if values is None:
            values = _derive_vocabulary(profile, field)
        if values is None:
            values = _extract_vocabulary(profile, field)
        if values is None:
            observed_fields.append(field)
        else:
            profile_vocabulary[field] = {"values": values, "source": "profile"}

    return profile_vocabulary, observed_fields


def fetch_observed_vocabularies(
    conn: sqlite3.Connection, profile_id: str, fields: list[str]
) -> dict[str, list[str]]:
    """Distinct non-NULL values `fields` have actually taken, read from
    `listings.spec_json` (there is no per-field column for a spec field -
    `bucket_key` is built FROM spec, not stored component-wise, design.md
    §5). One scan of this profile's ok/partial listings - rejected/
    not_target rows always carry `spec_json = '{}'` (store_spec()'s own
    contract) and contribute nothing, so they're excluded from the scan
    rather than parsed and discarded. Reads spec_json in Python via
    parse_spec_json(), the same shared parser scripts/score_active.py and
    reporting/panels.py's baseline_queue() already use - never a second,
    SQL-side JSON extraction the rest of this codebase doesn't use
    elsewhere.

    A real database dependency, unlike resolve_profile_vocabulary() above -
    the caller (server.py's TTL-rebuild accessor, V1.0 prompt 2b) is
    responsible for deciding how often to call this and what to do with
    an empty or failed result; this function itself has no notion of
    caching, staleness, or a fallback value.
    """
    found: dict[str, set[str]] = {field: set() for field in fields}
    rows = conn.execute(
        "SELECT spec_json FROM listings WHERE profile_id = ? "
        "AND spec_status IN ('ok', 'partial') AND spec_json IS NOT NULL",
        (profile_id,),
    ).fetchall()
    for (spec_json,) in rows:
        spec = parse_spec_json(spec_json)
        for field in fields:
            value = spec.get(field)
            if value is not None:
                found[field].add(str(value))
    return {field: sorted(values) for field, values in found.items()}


def build_bucket_vocabulary(
    profile: Profile, conn: sqlite3.Connection | None
) -> dict[str, dict]:
    """{field: {"values": [...], "source": "profile" | "observed"}} for
    every field in VOCABULARY_FIELDS - a one-shot composition of
    resolve_profile_vocabulary() + fetch_observed_vocabularies() for a
    caller that wants the whole thing in one call and doesn't need TTL
    caching (V1.0 prompt 2a's original shape; V1.0 prompt 2b's server.py
    no longer calls this directly for its own runtime state - see that
    module's `_current_bucket_vocabulary()` - but keeps it as the
    single-call reference implementation tests compare against, L13).
    `conn` may be None - every field needing the observed fallback then
    gets an empty vocabulary rather than a query attempt.
    """
    profile_vocabulary, observed_fields = resolve_profile_vocabulary(profile)
    result: dict[str, dict] = dict(profile_vocabulary)
    for field in observed_fields:
        result[field] = {"values": [], "source": "observed"}

    if observed_fields and conn is not None:
        observed = fetch_observed_vocabularies(conn, profile.id, observed_fields)
        for field in observed_fields:
            result[field]["values"] = observed.get(field, [])

    return result
