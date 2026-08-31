"""Tests for dealwatch.normalize.engine against the real
profiles/thinkpad-t14.yaml, using real listing titles from
tests/fixtures/titles.txt (project convention: every reject/require rule
gets a test with the title that motivated it).

Titles are the only thing that varies per test; subtitle/condition_id
default to None/None unless a test overrides them (a reject rule keyed on
condition_id needs one, and Browse returns no subtitle at all - design.md
§5.1 - so subtitle is always None in practice).
"""

from pathlib import Path

import pytest

from dealwatch.engine.collector import load_profile
from dealwatch.normalize.engine import (
    NOT_TARGET,
    OK,
    PARTIAL,
    REJECTED,
    ProfileCompileError,
    compile_profile,
    normalize,
)
from dealwatch.normalize.schema import (
    DeriveRule,
    ExtractField,
    ExtractRule,
    MatchRule,
    Profile,
    SearchConfig,
    TierBreak,
    TierField,
)


def _load_titles() -> list[str]:
    lines = (Path(__file__).parent / "fixtures" / "titles.txt").read_text().splitlines()
    titles = (line.strip().lstrip("- ").strip() for line in lines)
    return [t for t in titles if t and not t.startswith("#")]


TITLES = _load_titles()
PROFILE = load_profile(Path(__file__).parent.parent / "profiles" / "thinkpad-t14.yaml")


def fields(title: str, *, subtitle: str | None = None, condition_id: int | None = None) -> dict:
    return {"title": title, "subtitle": subtitle, "condition_id": condition_id}


def title_containing(fragment: str) -> str:
    for title in TITLES:
        if fragment.lower() in title.lower():
            return title
    raise AssertionError(f"no fixture title contains {fragment!r}")


# ---------------------------------------------------------------------------
# The real profile compiles cleanly - a load-time smoke test, not a proof of
# anything beyond "the YAML parses and every regex/reference is valid."
# ---------------------------------------------------------------------------


def test_real_profile_compiles_without_error():
    compile_profile(PROFILE)


# ---------------------------------------------------------------------------
# Reject rules - each with the real title that motivated it.
# ---------------------------------------------------------------------------


def test_t14s_not_t14_rejects_t14s_listing():
    title = title_containing("T14s GEN 2a")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "t14s-not-t14"


def test_for_parts_condition_rejects_condition_7000():
    title = title_containing("For Parts Not Working")
    result = normalize(PROFILE, fields(title, condition_id=7000))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "for-parts-condition"


def test_for_parts_condition_does_not_reject_a_normal_condition():
    title = title_containing("For Parts Not Working")
    result = normalize(PROFILE, fields(title, condition_id=3000))
    assert result.reject_rule_id != "for-parts-condition"


def test_for_parts_text_rejects_cracked_as_is_title():
    title = title_containing("Cracked Screen AS IS")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "for-parts-text"


def test_barebones_rejects_no_hdd_title():
    title = title_containing("NO HDD/OS")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "barebones"


def test_lot_listing_rejects_lot_of_five_title():
    title = title_containing("Lot of 5")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "lot-listing"


def test_accessory_rejects_dock_only_title():
    title = title_containing("Docking Station USB-C Dock Only")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "accessory"


def test_accessory_unless_does_not_reject_a_laptop_mentioning_a_dock():
    # The `unless` clause exists precisely so "T14 laptop with dock
    # included" doesn't get rejected as an accessory - construct that case
    # directly since no fixture title happens to combine both.
    title = "Lenovo ThinkPad T14 Gen 1 Laptop i5 16GB 256GB with dock included"
    result = normalize(PROFILE, fields(title))
    assert result.reject_rule_id != "accessory"


# ---------------------------------------------------------------------------
# Require rule.
# ---------------------------------------------------------------------------


def test_is_t14_fails_require_for_a_different_model():
    title = title_containing("T480")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == NOT_TARGET
    assert result.reject_rule_id is None


def test_reject_runs_before_require_t14s_is_rejected_not_not_target():
    # design.md §5: \bT14\b doesn't match "T14s" either, so if require ran
    # first this would ALSO look like a require failure. Reject must win.
    title = title_containing("T14s GEN 2a")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.spec_status != NOT_TARGET


# ---------------------------------------------------------------------------
# The no_os fix (barebones no longer rejects a complete "no OS" machine,
# and no_os is extracted as its own field instead).
# ---------------------------------------------------------------------------


def test_no_os_title_is_not_rejected_as_barebones():
    title = title_containing("No OS: Fair")
    result = normalize(PROFILE, fields(title))
    assert result.reject_rule_id != "barebones"
    assert result.spec_status != REJECTED


def test_no_os_extract_field_is_true_for_a_no_os_title():
    title = title_containing("No OS: Fair")
    result = normalize(PROFILE, fields(title))
    assert result.spec["no_os"] is True


def test_no_os_extract_field_is_false_when_absent():
    title = title_containing("Core i5-10310U @1.70GHz, 16GB RAM")
    result = normalize(PROFILE, fields(title))
    assert result.spec["no_os"] is False


# ---------------------------------------------------------------------------
# New cpu_family patterns + their generation/cpu_vendor derivation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment,expected_family,expected_generation,expected_vendor",
    [
        ("Core Ultra 5 135U", "intel-ultra-1", "5", "intel"),
        ("Core Ultra 7 265H", "intel-ultra-2", "6", "intel"),
        ("Ryzen 5 PRO 6650U", "amd-ryzen-6000", "3", "amd"),
        ("Ryzen 7 PRO 7840U", "amd-ryzen-7000", "4", "amd"),
        ("Ryzen 7 PRO 8840U", "amd-ryzen-8000", "5", "amd"),
        ("Ryzen AI 7 PRO 360", "amd-ryzen-ai", "6", "amd"),
    ],
)
def test_new_cpu_family_pattern_derives_generation_and_vendor(
    fragment, expected_family, expected_generation, expected_vendor
):
    title = title_containing(fragment)
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == expected_family
    # generation comes from the title's own "Gen N" text on these fixtures,
    # not the derive rule - assert the derive rule independently below.
    assert result.spec["cpu_vendor"] == expected_vendor


@pytest.mark.parametrize(
    "title,expected_generation",
    [
        ("Lenovo ThinkPad T14 Intel Core Ultra 5 135U 16GB 256GB", "5"),
        ("Lenovo ThinkPad T14 Intel Core Ultra 7 265H 16GB 256GB", "6"),
        ("Lenovo ThinkPad T14 AMD Ryzen 5 PRO 6650U 16GB 256GB", "3"),
        ("Lenovo ThinkPad T14 AMD Ryzen 7 PRO 7840U 16GB 256GB", "4"),
        ("Lenovo ThinkPad T14 AMD Ryzen 7 PRO 8840U 16GB 256GB", "5"),
        ("Lenovo ThinkPad T14 AMD Ryzen AI 7 PRO 360 16GB 256GB", "6"),
    ],
)
def test_generation_derive_rule_fills_in_when_title_has_no_gen_marker(title, expected_generation):
    # Isolates the derive rule from extraction: none of these titles say
    # "Gen N" anywhere, so generation starts null after extraction and the
    # derive rule is the only thing that can fill it in. The fixture
    # titles used above for the cpu_family/vendor test all DO say "Gen N",
    # so that test alone wouldn't prove the derive rule actually runs -
    # extraction would already have filled generation first.
    result = normalize(PROFILE, fields(title))
    assert result.spec["generation"] == expected_generation


def test_ryzen_ai_is_not_misread_as_a_digit_series_family():
    title = title_containing("Ryzen AI 7 PRO 360")
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "amd-ryzen-ai"
    assert result.spec["cpu_family"] not in (
        "amd-ryzen-6000", "amd-ryzen-7000", "amd-ryzen-8000",
    )


# ---------------------------------------------------------------------------
# spec_status assignment and bucket_key rendering, using small synthetic
# profiles - these are mechanical/boundary checks, not rule-motivation
# tests, so real fixture titles aren't required for them.
# ---------------------------------------------------------------------------


def _tiny_profile(**overrides) -> Profile:
    base = dict(
        id="tiny",
        name="Tiny",
        search=SearchConfig(queries=["x"]),
        reject=[],
        require=[],
        extract={
            "generation": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"gen\s*(\d)", value="{1}")],
                default=None,
            ),
            "cpu_family": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"(i[357])", value="{1}")],
                default=None,
            ),
            "ram_gb": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"(\d+)gb ram", value="{1}", apply="to_int")],
                default=None,
            ),
        },
        derive=[],
        tiers={
            "ram_tier": TierField(
                from_field="ram_gb",
                breaks=[
                    TierBreak(max=8, label="8"),
                    TierBreak(max=16, label="16"),
                    TierBreak(min=17, label="32+"),
                ],
            )
        },
        bucket_key=["generation", "cpu_family", "ram_tier"],
        bucket_require=["generation", "cpu_family"],
    )
    base.update(overrides)
    return Profile(**base)


def test_spec_status_ok_when_all_bucket_require_fields_present():
    profile = _tiny_profile()
    result = normalize(profile, fields("gen1 i5 16gb ram"))
    assert result.spec_status == OK
    assert result.bucket_key == "1|i5|16"


def test_spec_status_partial_when_a_bucket_require_field_is_missing():
    profile = _tiny_profile()
    result = normalize(profile, fields("gen1 16gb ram"))  # no cpu family
    assert result.spec_status == PARTIAL
    assert result.bucket_key == "1|?|16"


def test_bucket_key_renders_missing_non_required_component_as_question_mark():
    profile = _tiny_profile()
    result = normalize(profile, fields("gen1 i5"))  # no ram at all
    assert result.spec_status == OK  # bucket_require doesn't need ram_gb
    assert result.bucket_key == "1|i5|?"


def test_bucket_key_is_none_when_rejected():
    profile = _tiny_profile(
        reject=[MatchRule(id="r1", where="title", any=["gen1"], reason="test")]
    )
    result = normalize(profile, fields("gen1 i5 16gb ram"))
    assert result.spec_status == REJECTED
    assert result.bucket_key is None


def test_bucket_key_is_none_when_not_target():
    profile = _tiny_profile(
        require=[MatchRule(id="req1", where="title", any=["zzz-never-matches"], reason="test")]
    )
    result = normalize(profile, fields("gen1 i5 16gb ram"))
    assert result.spec_status == NOT_TARGET
    assert result.bucket_key is None


def test_null_tier_source_yields_null_tier_not_a_crash():
    profile = _tiny_profile()
    result = normalize(profile, fields("gen1 i5"))  # ram_gb None
    assert result.spec["ram_tier"] is None


def test_tier_break_boundaries_are_inclusive():
    profile = _tiny_profile()
    exactly_eight = normalize(profile, fields("gen1 i5 8gb ram"))
    exactly_sixteen = normalize(profile, fields("gen1 i5 16gb ram"))
    seventeen = normalize(profile, fields("gen1 i5 17gb ram"))
    assert exactly_eight.spec["ram_tier"] == "8"
    assert exactly_sixteen.spec["ram_tier"] == "16"
    assert seventeen.spec["ram_tier"] == "32+"


def test_derive_only_fills_null_unless_overwrite():
    profile = _tiny_profile(
        extract={
            "generation": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"gen\s*(\d)", value="{1}")],
                default=None,
            ),
        },
        derive=[DeriveRule(field="generation", when={}, value="OVERRIDDEN", overwrite=False)],
        tiers={},
        bucket_key=["generation"],
        bucket_require=["generation"],
    )
    # generation already extracted as "1" - a non-overwrite derive rule
    # matching everything (when={}) must not clobber it.
    result = normalize(profile, fields("gen1 i5"))
    assert result.spec["generation"] == "1"


def test_derive_overwrite_true_replaces_an_existing_value():
    profile = _tiny_profile(
        extract={
            "generation": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"gen\s*(\d)", value="{1}")],
                default=None,
            ),
        },
        derive=[DeriveRule(field="generation", when={}, value="OVERRIDDEN", overwrite=True)],
        tiers={},
        bucket_key=["generation"],
        bucket_require=["generation"],
    )
    result = normalize(profile, fields("gen1 i5"))
    assert result.spec["generation"] == "OVERRIDDEN"


def test_derive_startswith_condition():
    profile = _tiny_profile(
        extract={
            "cpu_family": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"(amd-\w+|intel-\w+)", value="{1}")],
                default=None,
            ),
        },
        derive=[DeriveRule(field="cpu_vendor", when={"cpu_family": {"startswith": "amd"}}, value="amd")],
        tiers={},
        bucket_key=["cpu_family"],
        bucket_require=["cpu_family"],
    )
    matched = normalize(profile, fields("amd-ryzen"))
    unmatched = normalize(profile, fields("intel-core"))
    assert matched.spec["cpu_vendor"] == "amd"
    assert unmatched.spec.get("cpu_vendor") is None


def test_extract_apply_function_converts_to_int_not_string():
    profile = _tiny_profile()
    result = normalize(profile, fields("gen1 i5 16gb ram"))
    assert result.spec["ram_gb"] == 16
    assert isinstance(result.spec["ram_gb"], int)


def test_extract_first_matching_rule_wins_not_the_last():
    profile = _tiny_profile(
        extract={
            "cpu_family": ExtractField(
                from_field="title",
                rules=[
                    ExtractRule(pattern=r"i5", value="first-rule-wins"),
                    ExtractRule(pattern=r"i5", value="should-never-see-this"),
                ],
                default=None,
            ),
        },
        tiers={},
        bucket_key=["cpu_family"],
        bucket_require=["cpu_family"],
    )
    result = normalize(profile, fields("i5 laptop"))
    assert result.spec["cpu_family"] == "first-rule-wins"


def test_extract_default_used_when_no_rule_matches():
    profile = _tiny_profile(
        extract={
            "cpu_family": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"i5", value="i5-found")],
                default="unknown",
            ),
        },
        tiers={},
        bucket_key=["cpu_family"],
        bucket_require=[],
    )
    result = normalize(profile, fields("i7 laptop"))  # no i5 present
    assert result.spec["cpu_family"] == "unknown"


def test_matching_is_case_insensitive():
    # Case-insensitive means the PATTERN matches regardless of input case -
    # it says nothing about normalizing a captured group's case, so this
    # uses a literal-value rule (touchscreen-style, no capture group) to
    # isolate that specific claim rather than getting tangled up in what a
    # `{1}` substitution should do with mixed-case input.
    profile = _tiny_profile(
        extract={
            "generation": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"gen\s*(\d)", value="{1}")],
                default=None,
            ),
            "cpu_family": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"i5", value="has-i5")],
                default=None,
            ),
        },
        tiers={},
        bucket_key=["generation", "cpu_family"],
        bucket_require=["generation", "cpu_family"],
    )
    result = normalize(profile, fields("GEN1 I5 16GB RAM"))
    assert result.spec["generation"] == "1"
    assert result.spec["cpu_family"] == "has-i5"


def test_where_accepts_a_list_of_fields_and_matches_any_of_them():
    profile = _tiny_profile(
        reject=[MatchRule(id="r1", where=["title", "subtitle"], any=["bad"], reason="t")]
    )
    hit_via_subtitle = normalize(profile, fields("gen1 i5", subtitle="this one is bad"))
    assert hit_via_subtitle.spec_status == REJECTED


def test_first_matching_reject_rule_wins_the_rest_are_not_evaluated():
    profile = _tiny_profile(
        reject=[
            MatchRule(id="first", where="title", any=["gen1"], reason="t"),
            MatchRule(id="second", where="title", any=["gen1"], reason="t"),
        ]
    )
    result = normalize(profile, fields("gen1 i5"))
    assert result.reject_rule_id == "first"


# ---------------------------------------------------------------------------
# Profile loading is strict (compile_profile) - each of these must raise at
# load time, never silently no-match at runtime.
# ---------------------------------------------------------------------------


def test_invalid_regex_is_a_compile_time_error():
    profile = _tiny_profile(
        reject=[MatchRule(id="bad", where="title", any=["(unclosed"], reason="t")]
    )
    with pytest.raises(ProfileCompileError):
        compile_profile(profile)


def test_unknown_apply_function_is_a_compile_time_error():
    profile = _tiny_profile(
        extract={
            "x": ExtractField(
                from_field="title",
                rules=[ExtractRule(pattern=r"(\d+)", value="{1}", apply="not_a_real_function")],
                default=None,
            )
        },
        tiers={},
        bucket_key=[],
        bucket_require=[],
    )
    with pytest.raises(ProfileCompileError):
        compile_profile(profile)


def test_bucket_key_naming_an_unproduced_field_is_a_compile_time_error():
    profile = _tiny_profile(bucket_key=["nonexistent_field"])
    with pytest.raises(ProfileCompileError):
        compile_profile(profile)


def test_bucket_require_naming_an_unproduced_field_is_a_compile_time_error():
    profile = _tiny_profile(bucket_require=["nonexistent_field"])
    with pytest.raises(ProfileCompileError):
        compile_profile(profile)


def test_tier_from_naming_an_unproduced_field_is_a_compile_time_error():
    # bucket_key/bucket_require must not still reference "ram_tier" here -
    # tiers no longer defines it, so that reference would raise its own
    # (correct, but different) error and mask whether THIS check works.
    profile = _tiny_profile(
        tiers={"bad_tier": TierField(from_field="nonexistent_field", breaks=[TierBreak(max=1, label="x")])},
        bucket_key=["generation", "cpu_family"],
        bucket_require=["generation", "cpu_family"],
    )
    with pytest.raises(ProfileCompileError):
        compile_profile(profile)


def test_rule_with_neither_any_nor_in_is_a_compile_time_error():
    profile = _tiny_profile(
        reject=[MatchRule(id="empty", where="title", reason="t")]
    )
    with pytest.raises(ProfileCompileError):
        compile_profile(profile)


def test_rule_with_both_any_and_in_is_a_compile_time_error():
    profile = _tiny_profile(
        reject=[MatchRule(id="both", where="condition_id", any=["x"], **{"in": [7000]}, reason="t")]
    )
    with pytest.raises(ProfileCompileError):
        compile_profile(profile)
