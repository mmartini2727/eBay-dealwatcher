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
    # This rule keys on condition_id, not title text, so any real title
    # works here - reuse an ordinary T14 title and vary condition_id.
    title = title_containing("Core i5-10310U @1.70GHz, 16GB RAM")
    result = normalize(PROFILE, fields(title, condition_id=7000))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "for-parts-condition"


def test_for_parts_condition_does_not_reject_a_normal_condition():
    title = title_containing("Core i5-10310U @1.70GHz, 16GB RAM")
    result = normalize(PROFILE, fields(title, condition_id=3000))
    assert result.reject_rule_id != "for-parts-condition"


def test_for_parts_text_rejects_cracked_as_is_title():
    # No real "cracked"/"as-is" title on file yet (see titles.txt) -
    # constructed directly rather than inventing a fake fixture entry.
    title = "Lenovo ThinkPad T14 Gen 1 i5 16GB 256GB - Cracked Screen, AS IS"
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "for-parts-text"


def test_barebones_rejects_no_hdd_title():
    title = title_containing("NO HDD/OS")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "barebones"


def test_lot_listing_rejects_lot_of_five_title():
    # No real "lot of N" title on file yet - constructed directly.
    title = "Lot of 5 Lenovo ThinkPad T14 Gen 1 i5 16GB 256GB - untested"
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "lot-listing"


def test_accessory_rejects_dock_only_title():
    # No real dock-only title on file yet - constructed directly. Avoids
    # "laptop"/i-series/ryzen text on purpose - those are exactly what the
    # `unless` clause checks for, and this listing genuinely isn't a laptop.
    title = "Lenovo ThinkPad T14 Docking Station USB-C Dock Only, Genuine OEM"
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


def test_accessory_unless_does_not_reject_a_core_ultra_listing_mentioning_an_accessory():
    # V0.7a fix: 'ultra' was missing from the unless CPU alternation, so any
    # Core Ultra listing whose feature list mentioned an accessory word
    # (dock/charger/case/etc, without also saying "laptop") was silently
    # rejected. No fixture title happens to combine "Ultra" with an
    # accessory word AND omit "laptop"/i-series/ryzen text, so constructed
    # directly - the real motivating title (rejected via `webcam` alone,
    # fixed separately below) is in titles.txt.
    title = "Lenovo ThinkPad T14 Gen 5 Ultra 7 165U 16GB 512GB with charger"
    result = normalize(PROFILE, fields(title))
    assert result.reject_rule_id != "accessory"


def test_accessory_webcam_and_fan_keywords_no_longer_reject_anything():
    # Isolates the pattern removal itself (as opposed to the unless-clause
    # fix above): a title whose ONLY accessory-flavored word is "webcam" or
    # "fan" must no longer be rejected as an accessory at all, independent
    # of CPU wording. Neither is sold as a standalone T14 part in any real
    # volume, and both appear routinely in ordinary feature lists.
    # Deliberately no "i5"/"ryzen"/"ultra"/"laptop" text - those are exactly
    # what `unless` checks for, and including one would rescue the listing
    # regardless of whether the webcam/fan patterns are still in the
    # any-list, masking the very thing this test claims to cover.
    webcam_title = "Lenovo ThinkPad T14 Gen 1 16GB 256GB with HD Webcam"
    fan_title = "Lenovo ThinkPad T14 Gen 1 16GB 256GB Fan Runs Quiet"
    assert normalize(PROFILE, fields(webcam_title)).reject_rule_id != "accessory"
    assert normalize(PROFILE, fields(fan_title)).reject_rule_id != "accessory"


def test_accessory_no_longer_rejects_real_core_ultra_listing_mentioning_webcam():
    # The actual bug report, as an end-to-end regression check - NOT an
    # isolated-mechanism test. This title has both "webcam" (any-list) and
    # "Ultra 7" (unless-list) in it, so either fix alone rescues it; the two
    # tests above already sabotage-check each mechanism independently.
    # Sabotage-checked: restoring `webcam` to the any-list alone does NOT
    # turn this test red, because the `ultra` unless-clause rescues it too.
    title = title_containing("Ultra 7 165U 14")
    result = normalize(PROFILE, fields(title))
    assert result.reject_rule_id != "accessory"
    assert result.spec_status != REJECTED


# ---------------------------------------------------------------------------
# Require rule.
# ---------------------------------------------------------------------------


def test_is_t14_fails_require_for_a_different_model():
    title = title_containing("L15Gen 4")
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
# V0.7a fix 3: is-t14 / t14s-not-t14 widened to accept a space between
# "T" and "14" ('\bT[\s-]?14\b'). Two real listings were lost to this.
# ---------------------------------------------------------------------------


def test_is_t14_now_matches_a_spaced_t_14_gen_1_title():
    # Previously not_target: '\bT14\b' does not match "T 14". This is the
    # real listing the V0.7a report surfaced as lost to spacing.
    title = title_containing("Thinkpad T 14 Gen 1")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status != NOT_TARGET


def test_is_t14_now_matches_a_spaced_t_14_gen_4_title():
    # The other real spacing loss the report surfaced, and a Gen 4 machine
    # specifically (the task calls this out - a real gap in a generation
    # this profile actively wants comps for).
    title = title_containing("Lenovo T 14 Gen 4")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status != NOT_TARGET
    assert result.spec["generation"] == "4"
    assert result.spec["cpu_family"] == "intel-13th"  # i5-1335U, existing pattern


def test_t14s_not_t14_widened_to_reject_a_spaced_t_14s_title():
    # require widening alone would let spaced "T 14s" listings pass through
    # as if they were T14 - the reject pattern must widen in lockstep. No
    # real spaced-T14s title on file; this is the literal example the
    # V0.7a task spec names for this fix.
    title = "Lenovo ThinkPad T 14s Gen 2"
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == REJECTED
    assert result.reject_rule_id == "t14s-not-t14"


def test_t14sgen2_with_no_space_and_no_boundary_still_not_target():
    # Explicitly NOT chased (per the V0.7a task): "T14SGen2" has no word
    # boundary between "4" and "S", so \b never lands after "14" for either
    # the widened require or the widened reject pattern. Confirms the
    # widening didn't change this listing's classification.
    title = title_containing("T14SGen2")
    result = normalize(PROFILE, fields(title))
    assert result.spec_status == NOT_TARGET
    assert result.reject_rule_id is None


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
    # No real fixture title carries these CPU families yet (Core Ultra /
    # Ryzen 6000-8000/AI postdate most of the collected history) -
    # constructed directly rather than inventing fake "real" fixture
    # entries. generation comes from the title's own "Gen N" text on these
    # constructed titles, not the derive rule - assert the derive rule
    # independently below.
    title = f"Lenovo ThinkPad T14 Gen {expected_generation} {fragment} 16GB 256GB"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == expected_family
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
    # No real fixture title on file yet - see the parametrized test above.
    title = "Lenovo ThinkPad T14 Gen 6 AMD Ryzen AI 7 PRO 360 16GB 256GB"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "amd-ryzen-ai"
    assert result.spec["cpu_family"] not in (
        "amd-ryzen-6000", "amd-ryzen-7000", "amd-ryzen-8000",
    )


# ---------------------------------------------------------------------------
# V0.7c fix 2: the AMD series digit ([3579]) is optional across the
# model-number patterns - "Ryzen PRO 8540U" (no series digit at all) was
# coming back cpu_family=None. The four-digit model number stays mandatory.
# ---------------------------------------------------------------------------


def test_ryzen_pro_with_no_series_digit_matches_the_real_listing():
    title = title_containing("Ryzen PRO 8540U")
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "amd-ryzen-8000"


def test_ryzen_pro_with_no_series_digit_derives_generation_via_derive_rule():
    # Isolates the derive rule from extraction, same convention as
    # test_generation_derive_rule_fills_in_when_title_has_no_gen_marker
    # above: no "Gen N" text anywhere in this constructed title, so
    # generation starts null after extraction and only the derive rule
    # (keyed on cpu_family) can fill it in.
    title = "Lenovo ThinkPad T14 AMD Ryzen PRO 8540U 16GB 256GB"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "amd-ryzen-8000"
    assert result.spec["generation"] == "5"


def test_ryzen_5_pro_8540u_with_series_digit_still_matches():
    # Regression check: the already-working case (series digit present)
    # must still work once the digit is made optional, not just the new
    # no-digit case.
    title = "Lenovo ThinkPad T14 AMD Ryzen 5 PRO 8540U 16GB 256GB"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "amd-ryzen-8000"


def test_ryzen_5_pro_with_no_model_number_still_yields_none():
    # The line this fix must not cross: a bare CPU marker with no model
    # number is a separate inference decision (out of scope), not a
    # pattern fix. Making the series digit optional must not turn this
    # into an accidental match.
    title = "Lenovo ThinkPad T14 AMD Ryzen 5 PRO 16GB 256GB"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] is None


@pytest.mark.parametrize(
    "model_number,expected_family",
    [
        ("4680U", "amd-ryzen-4000"),
        ("5625U", "amd-ryzen-5000"),
        ("6850U", "amd-ryzen-6000"),
        ("7735U", "amd-ryzen-7000"),
    ],
)
def test_ryzen_pro_with_no_series_digit_matches_other_series_for_consistency(
    model_number, expected_family
):
    # The task named 8000 as the motivating case but asked for the same
    # treatment on 4000/5000/6000/7000 "for consistency - the same seller
    # habit will show up on those." No real fixture title for these yet,
    # constructed directly.
    title = f"Lenovo ThinkPad T14 AMD Ryzen PRO {model_number} 16GB 256GB"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == expected_family


def test_ryzen_ai_9_365_still_resolves_to_amd_ryzen_ai_not_a_loosened_digit_pattern():
    # The exact title named in the V0.7c task: confirms the ryzen-ai
    # pattern still takes precedence now that the digit-series patterns
    # below it no longer require a series digit either.
    title = "Lenovo ThinkPad T14 AMD Ryzen AI 9 365 16GB 256GB"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "amd-ryzen-ai"


# ---------------------------------------------------------------------------
# V0.7a fix 2: cpu_family ordinal fallbacks ("i5 10th Gen", "12Gen Intel
# i5", bare "10610U" with no i-series adjacency). 204/1094 real listings
# came back partial with cpu_family null before this fix; real titles below.
# ---------------------------------------------------------------------------


def test_cpu_family_ordinal_pattern_matches_i_series_space_ordinal():
    # Isolates the i-series+ordinal-th pattern specifically: "i5 10th
    # 256GB" has no "gen" text anywhere near the CPU segment (the standalone
    # bare-ordinal-plus-gen pattern can't rescue this one). Sabotage-checked:
    # removing this pattern alone turns this test red without touching the
    # other ordinal patterns.
    title = title_containing("i5 10th 256GB")
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "intel-10th"


def test_cpu_family_i_series_plus_ordinal_th_and_gen_do_not_cross_contaminate():
    # A different real shape of the same pattern - i-series directly
    # adjacent to "10th Gen" - plus a check that the CPU's ordinal-th text
    # doesn't leak into the (unrelated) ThinkPad generation field, which
    # here comes from "G1" instead. NOT an isolated-mechanism test: the
    # standalone bare-ordinal-plus-gen pattern also matches "10th Gen" here,
    # so sabotaging the i-series+ordinal-th pattern alone does NOT turn this
    # test red (confirmed) - test_..._space_ordinal above covers that
    # isolation instead.
    title = title_containing("i7 10th Gen")
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "intel-10th"
    assert result.spec["generation"] == "1"  # from "G1" in the title


def test_cpu_family_ordinal_pattern_reuses_existing_generation_derive_rule():
    title = title_containing("Intel Core i5 10th Gen., 16GB Ram")
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "intel-10th"
    # This title has no "Gen N"/"GN" marker at all (it's "1st Gen" in
    # english prose, which no pattern targets - a known, separate gap) -
    # generation must come from the intel-10th derive rule, proving the
    # ordinal pattern produces a value the EXISTING derive rules recognize,
    # not a new disjoint family that needs its own derive rule.
    assert result.spec["generation"] == "1"


def test_cpu_family_bare_ordinal_gen_pattern_does_not_collide_with_thinkpad_generation():
    # The critical case: "Gen3" (ThinkPad generation) and "12Gen" (CPU
    # ordinal) appear in the same title. generation must come from "Gen3"
    # only; cpu_family's bare-ordinal pattern must independently pick up
    # "12Gen" without the two fields cross-contaminating each other.
    title = title_containing("Gen3 Touchscreen")
    result = normalize(PROFILE, fields(title))
    assert result.spec["generation"] == "3"
    assert result.spec["cpu_family"] == "intel-12th"


def test_bare_ordinal_gen_pattern_does_not_fire_on_thinkpad_generation_alone():
    # "T14 Gen 1" alone (no CPU info) must not accidentally set cpu_family:
    # \b(1[0-3])(?:th)?\s*gen\b requires the digit BEFORE "gen", and "T14"'s
    # "14" doesn't qualify (1[0-3] rejects the second digit "4"). Constructed
    # directly - no fixture title is this minimal.
    title = "Lenovo ThinkPad T14 Gen 1"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] is None


def test_cpu_family_bare_model_number_pattern_matches_without_i_series_adjacency():
    # "Intel Core i7 vPRO 10610U" - "vPRO" sits between i7 and the model
    # number, so the i[3579]-anchored patterns can't reach it. The bare
    # model-number fallback (mandatory U/H/G suffix) is what catches this.
    title = title_containing("vPRO 10610U")
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] == "intel-10th"


@pytest.mark.parametrize("fragment", ["1TB", "512GB", "2.80GHz"])
def test_bare_model_number_pattern_does_not_match_storage_or_clock_speed(fragment):
    # The mandatory U/H/G suffix is what's supposed to prevent this. None
    # of these end in U/H/G immediately after a 1[0-3]xx-shaped run of
    # digits, so cpu_family must stay null.
    title = f"Lenovo ThinkPad T14 Gen 1 {fragment} laptop, no CPU listed"
    result = normalize(PROFILE, fields(title))
    assert result.spec["cpu_family"] is None


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
