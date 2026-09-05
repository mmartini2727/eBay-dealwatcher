"""Profile config schema - the shape of `profiles/*.yaml`.

Models identity fields, `search`, the normalization pipeline
(reject/require/extract/derive/tiers/bucket_key/bucket_require - design.md
§5), and `seed_baselines` (V0.8b, §5.6). `extra="ignore"` stays on Profile
because `scoring` is still only a loose dict and `alerts` is entirely
unmodeled (V0.9); rejecting them now would mean today's client can't load
a real profile at all.

This module only models *shape* - what keys/types are allowed. Semantic
validation (does a regex compile, does an `apply:` name a real function,
does a bucket_key field actually get produced by something) is
dealwatch.normalize.engine.compile_profile's job, not this one's.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class PollConfig(BaseModel):
    interval_minutes: int = 5
    sweep_interval_minutes: int = 60
    sort: str | None = None
    # V0.7c: was a collector.py module constant. The sweep's pagination
    # ceiling (sweep_page_limit * sweep_max_pages) must exceed the active
    # set, or listings past the horizon are never seen and get marked gone
    # on a miss-count timer - see CLAUDE.md's Traps. Defaults give a
    # 2,000-listing ceiling against a measured ~1,013 active set.
    sweep_page_limit: int = 200
    sweep_max_pages: int = 10


class SearchConfig(BaseModel):
    queries: list[str]
    # Best-effort only - Browse's negative-term handling in `q` is
    # inconsistent, so this is not authoritative filtering. See
    # profiles/thinkpad-t14.yaml's comment on this field.
    query_exclude: list[str] = []
    category_ids: list[str] = []
    # eBay's own filter grammar, e.g. {"price": [80, 1200], "conditionIds":
    # [2000, 3000]} - kept as a loose dict rather than a typed model because
    # it's passed straight through to build_filter_string().
    filters: dict[str, Any] = {}
    poll: PollConfig = PollConfig()


class MatchRule(BaseModel):
    """One reject or require rule - same shape for both (design.md §5).

    Exactly one of `any` (regex patterns) or `in` (a value-membership
    check, e.g. against condition_id) must be given; engine.compile_profile
    enforces that, not this model, since it's a semantic rule rather than a
    shape one.
    """

    model_config = ConfigDict(populate_by_name=True)

    id: str
    where: str | list[str]
    any: list[str] = []
    unless: list[str] = []
    in_values: list[Any] | None = Field(default=None, alias="in")
    reason: str


class ExtractRule(BaseModel):
    pattern: str
    # A string with {1}/{2} capture-group placeholders, or a literal value
    # (bool/str/int) used as-is when the pattern matches - see
    # profiles/thinkpad-t14.yaml's touchscreen field for the literal case.
    value: Any
    apply: str | None = None


class ExtractField(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_field: str = Field(alias="from")
    rules: list[ExtractRule]
    default: Any = None


class DeriveRule(BaseModel):
    field: str
    # {field_name: literal_value} for equality, or
    # {field_name: {"startswith": prefix}} - engine.py interprets this, not
    # a full sub-model, since it's a two-case mini-language.
    when: dict[str, Any]
    value: Any
    overwrite: bool = False


class TierBreak(BaseModel):
    max: int | None = None
    min: int | None = None
    label: str


class TierField(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    from_field: str = Field(alias="from")
    breaks: list[TierBreak]


class SeedBaselineEntry(BaseModel):
    """One seed_baselines entry (design.md §5.6, V0.8b) - a hand-authored
    fallback for a bucket the survival baseline hasn't reached min_samples
    for yet. `match` is spec-field-name -> value; an empty dict is the
    universal fallback and matches everything. p25/p50 are DOLLARS here,
    matching how the profile is authored and read by a human - conversion
    to integer cents happens once, in engine/scoring.py's compile step, not
    here: this module only models shape (see the file docstring), and cents
    is a scoring-domain decision, not a shape one.

    These were authored as FAST-SALE prices ("at this price it gets
    sniped") - deliberately the same quantity layer 3 (baselines.py)
    computes, not "market value." Nothing in the ladder applies a
    conversion factor between the two; they're the same thing by
    construction.
    """

    match: dict[str, Any] = {}
    p25: float
    p50: float


class Profile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    enabled: bool = True
    provider: str = "ebay"
    schema_version: int = 1
    search: SearchConfig

    reject: list[MatchRule] = []
    require: list[MatchRule] = []
    extract: dict[str, ExtractField] = {}
    derive: list[DeriveRule] = []
    tiers: dict[str, TierField] = {}
    bucket_key: list[str] = []
    bucket_require: list[str] = []
    # Exposed as a raw dict (scripts/normalize_report.py reads
    # min_samples), not interpreted here - actually scoring against it is
    # V0.8's job and explicitly out of scope for this module.
    scoring: dict[str, Any] = {}
    # V0.8b - see SeedBaselineEntry. Semantic validation (duplicate match
    # blocks) is engine/scoring.py's compile step, same division of labor
    # as reject/require/extract above.
    seed_baselines: list[SeedBaselineEntry] = []
