"""Generic profile interpreter (design.md §5): reject -> require -> extract
-> derive -> tiers -> bucket_key.

Pure: no database access, no collector wiring, no writes to spec_json,
bucket_key, or spec_status. raw_json is persisted per observation (V0.5),
so normalization is always recomputable from history - it never needs to
be on the write path for correctness, and the live-verified collector
stays untouched while these regexes churn against real titles. Backfill
and collector wiring are V0.7b, after the patterns stop moving.

Pipeline order is deliberate, not incidental: reject runs before require
because `\bT14\b` does not match "T14s" (no word boundary between "4" and
"s") - require-first would classify every T14s listing as not_target, and
the t14s-not-t14 reject rule would report zero hits forever.
"""

import re
from dataclasses import dataclass, field
from typing import Any

from dealwatch.normalize import functions
from dealwatch.normalize.schema import DeriveRule, ExtractField, MatchRule, Profile, TierField

# spec_status values the engine can produce. 'pending' and 'stale' are
# collector states (storage/sqlite.py) - the engine never emits them.
REJECTED = "rejected"
NOT_TARGET = "not_target"
OK = "ok"
PARTIAL = "partial"


class ProfileCompileError(ValueError):
    """Raised for a profile that can't be loaded safely: an invalid regex,
    an `apply:` naming a function that doesn't exist, or a bucket_key /
    bucket_require / tier `from:` naming a field nothing produces. A
    startup error, never a silent no-match at 2am."""


@dataclass
class SpecResult:
    spec: dict[str, Any]
    spec_status: str
    reject_rule_id: str | None
    bucket_key: str | None


@dataclass
class _CompiledMatchRule:
    id: str
    where: list[str]
    any_patterns: list[re.Pattern]
    unless_patterns: list[re.Pattern]
    in_values: list[Any] | None
    reason: str


@dataclass
class _CompiledExtractRule:
    pattern: re.Pattern
    value: Any
    apply: str | None


@dataclass
class _CompiledExtractField:
    from_field: str
    rules: list[_CompiledExtractRule]
    default: Any


@dataclass
class CompiledProfile:
    reject: list[_CompiledMatchRule] = field(default_factory=list)
    require: list[_CompiledMatchRule] = field(default_factory=list)
    extract: dict[str, _CompiledExtractField] = field(default_factory=dict)


def _compile_regex(pattern: str, context: str) -> re.Pattern:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        raise ProfileCompileError(f"{context}: invalid regex {pattern!r}: {exc}") from exc


def _where_fields(where: str | list[str]) -> list[str]:
    return [where] if isinstance(where, str) else where


def _compile_match_rule(rule: MatchRule) -> _CompiledMatchRule:
    has_any = bool(rule.any)
    has_in = rule.in_values is not None
    if has_any == has_in:  # both True or both False
        raise ProfileCompileError(
            f"rule {rule.id!r} must specify exactly one of `any` or `in`"
        )
    return _CompiledMatchRule(
        id=rule.id,
        where=_where_fields(rule.where),
        any_patterns=[_compile_regex(p, f"rule {rule.id!r}") for p in rule.any],
        unless_patterns=[_compile_regex(p, f"rule {rule.id!r} unless") for p in rule.unless],
        in_values=rule.in_values,
        reason=rule.reason,
    )


def _compile_extract_field(name: str, field_def: ExtractField) -> _CompiledExtractField:
    rules = []
    for rule in field_def.rules:
        if rule.apply is not None and rule.apply not in functions.FUNCTIONS:
            raise ProfileCompileError(
                f"extract field {name!r}: apply: {rule.apply!r} is not a known function"
            )
        rules.append(
            _CompiledExtractRule(
                pattern=_compile_regex(rule.pattern, f"extract field {name!r}"),
                value=rule.value,
                apply=rule.apply,
            )
        )
    return _CompiledExtractField(from_field=field_def.from_field, rules=rules, default=field_def.default)


def compile_profile(profile: Profile) -> CompiledProfile:
    """Validate and precompile a profile's normalization pipeline. Callers
    that load a profile (the report script, explain.py) should call this
    once up front to fail fast; normalize() also calls it internally, so a
    bad profile fails on the very first listing either way."""
    compiled = CompiledProfile(
        reject=[_compile_match_rule(r) for r in profile.reject],
        require=[_compile_match_rule(r) for r in profile.require],
        extract={name: _compile_extract_field(name, f) for name, f in profile.extract.items()},
    )

    producible = (
        set(profile.extract)
        | {rule.field for rule in profile.derive}
        | set(profile.tiers)
    )

    for tier_name, tier_def in profile.tiers.items():
        if tier_def.from_field not in producible:
            raise ProfileCompileError(
                f"tier {tier_name!r}: from: {tier_def.from_field!r} is not "
                "produced by extract, derive, or another tier"
            )

    for field_name in profile.bucket_key:
        if field_name not in producible:
            raise ProfileCompileError(
                f"bucket_key: {field_name!r} is not produced by extract, derive, or tiers"
            )

    for field_name in profile.bucket_require:
        if field_name not in producible:
            raise ProfileCompileError(
                f"bucket_require: {field_name!r} is not produced by extract, derive, or tiers"
            )

    return compiled


def _field_text(listing_fields: dict, field_name: str) -> str:
    value = listing_fields.get(field_name)
    return "" if value is None else str(value)


def _rule_fires(rule: _CompiledMatchRule, listing_fields: dict) -> bool:
    if rule.in_values is not None:
        matched = any(listing_fields.get(f) in rule.in_values for f in rule.where)
    else:
        matched = any(
            pattern.search(_field_text(listing_fields, f))
            for f in rule.where
            for pattern in rule.any_patterns
        )
    if not matched:
        return False

    if any(
        pattern.search(_field_text(listing_fields, f))
        for f in rule.where
        for pattern in rule.unless_patterns
    ):
        return False

    return True


def _render_value(value: Any, match: re.Match) -> Any:
    if isinstance(value, str) and "{" in value:
        return re.sub(r"\{(\d+)\}", lambda m: match.group(int(m.group(1))) or "", value)
    return value


def _extract_field(field_def: _CompiledExtractField, listing_fields: dict) -> Any:
    source = _field_text(listing_fields, field_def.from_field)
    for rule in field_def.rules:
        match = rule.pattern.search(source)
        if match is None:
            continue
        value = _render_value(rule.value, match)
        if rule.apply is not None:
            value = functions.FUNCTIONS[rule.apply](value)
        return value
    return field_def.default


def _when_matches(when: dict[str, Any], spec: dict) -> bool:
    for field_name, condition in when.items():
        actual = spec.get(field_name)
        if isinstance(condition, dict) and "startswith" in condition:
            if not (isinstance(actual, str) and actual.startswith(condition["startswith"])):
                return False
        elif actual != condition:
            return False
    return True


def _apply_derive(rules: list[DeriveRule], spec: dict, trace: list[str] | None) -> None:
    for rule in rules:
        if spec.get(rule.field) is not None and not rule.overwrite:
            continue
        if _when_matches(rule.when, spec):
            spec[rule.field] = rule.value
            if trace is not None:
                trace.append(f"derive[{rule.field}] = {rule.value!r} (when {rule.when!r})")


def _break_matches(brk, source: Any) -> bool:
    if brk.max is not None and source > brk.max:
        return False
    if brk.min is not None and source < brk.min:
        return False
    return True


def _apply_tier(tier_def: TierField, spec: dict) -> Any:
    source = spec.get(tier_def.from_field)
    if source is None:
        return None
    for brk in tier_def.breaks:
        if _break_matches(brk, source):
            return brk.label
    return None


def _build_bucket_key(fields: list[str], spec: dict) -> str | None:
    if not fields:
        return None
    return "|".join(
        "?" if spec.get(f) is None else str(spec.get(f)) for f in fields
    )


def _normalize(profile: Profile, listing_fields: dict, trace: list[str] | None) -> SpecResult:
    compiled = compile_profile(profile)

    for rule in compiled.reject:
        fired = _rule_fires(rule, listing_fields)
        if trace is not None:
            trace.append(f"reject[{rule.id}]: {'FIRED' if fired else 'no match'}")
        if fired:
            if trace is not None:
                trace.append(f"-> rejected by {rule.id!r}: {rule.reason}")
            return SpecResult(spec={}, spec_status=REJECTED, reject_rule_id=rule.id, bucket_key=None)

    for rule in compiled.require:
        satisfied = _rule_fires(rule, listing_fields)
        if trace is not None:
            trace.append(f"require[{rule.id}]: {'satisfied' if satisfied else 'FAILED'}")
        if not satisfied:
            if trace is not None:
                trace.append(f"-> not_target: failed require {rule.id!r}: {rule.reason}")
            return SpecResult(spec={}, spec_status=NOT_TARGET, reject_rule_id=None, bucket_key=None)

    spec: dict[str, Any] = {}
    for name, field_def in compiled.extract.items():
        spec[name] = _extract_field(field_def, listing_fields)
        if trace is not None:
            trace.append(f"extract[{name}] = {spec[name]!r}")

    _apply_derive(profile.derive, spec, trace)

    for tier_name, tier_def in profile.tiers.items():
        spec[tier_name] = _apply_tier(tier_def, spec)
        if trace is not None:
            trace.append(f"tier[{tier_name}] = {spec[tier_name]!r}")

    spec_status = OK if all(spec.get(f) is not None for f in profile.bucket_require) else PARTIAL
    bucket_key = _build_bucket_key(profile.bucket_key, spec) if profile.bucket_key else None

    if trace is not None:
        trace.append(f"-> spec_status={spec_status} bucket_key={bucket_key!r}")

    return SpecResult(spec=spec, spec_status=spec_status, reject_rule_id=None, bucket_key=bucket_key)


def normalize(profile: Profile, listing_fields: dict[str, Any]) -> SpecResult:
    """Run one listing's fields through profile's normalization pipeline.

    listing_fields is a plain dict (e.g. a mapped Listing's model_dump()) -
    `where:`/`from:` reference whatever keys are present by name, so the
    engine has no compile-time dependency on Listing's shape.
    """
    return _normalize(profile, listing_fields, trace=None)


def normalize_verbose(profile: Profile, listing_fields: dict[str, Any]) -> tuple[SpecResult, list[str]]:
    """Like normalize(), but also returns a human-readable trace of every
    pipeline stage - what explain.py prints."""
    trace: list[str] = []
    result = _normalize(profile, listing_fields, trace=trace)
    return result, trace
