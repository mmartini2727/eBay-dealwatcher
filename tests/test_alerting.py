"""Tests for dealwatch.engine.alerting (V0.9, design.md's dated entry).

evaluate() is pure aside from reading conn - real SQLite under tmp_path, no
mocking the persistence layer, same convention as every other storage-facing
test in this project. run_alert_cycle()'s tests mock the webhook by
monkeypatching dealwatch.notify.discord.send_alert directly (module-level
patch, not a client/transport injected through run_alert_cycle's own
signature - there is no pluggable-transport abstraction here on purpose,
CLAUDE.md) - no test in this file makes a real HTTP call.
"""

import asyncio
import json

import pydantic
import pytest

from dealwatch.engine import alerting
from dealwatch.engine.scoring import compile_seed_baselines
from dealwatch.normalize.engine import ProfileCompileError, SpecResult
from dealwatch.normalize.schema import AlertsConfig, AlertTrigger, PollConfig, Profile, SearchConfig
from dealwatch.storage.sqlite import connect, record_alert, record_sighting, store_spec

PROFILE_ID = "thinkpad-t14"
BUCKET = "1|intel-10th|16"


def run(coro):
    return asyncio.run(coro)


def make_alerts_config(**overrides) -> AlertsConfig:
    defaults = dict(
        webhook_env="DISCORD_WEBHOOK_TEST",
        dry_run=True,
        trigger=AlertTrigger(max_ratio_to_p25=1.00),
        max_price_usd=1000.0,
        realert_drop_pct=8,
        cooldown_minutes=60,
        max_per_cycle=10,
        include_variations=False,
        title_template="{generation} {cpu_family}",
        fields=["generation", "cpu_family"],
    )
    defaults.update(overrides)
    return AlertsConfig(**defaults)


_UNSET = object()


def make_profile(*, alerts=_UNSET, seed_baselines=None) -> Profile:
    # alerts defaults to a fresh make_alerts_config() when the caller
    # doesn't pass it at all - but alerts=None must stay None (a profile
    # with no alerts block), not silently become a default config. A plain
    # `alerts if alerts is not None else make_alerts_config()` can't tell
    # those two cases apart; the sentinel can.
    return Profile(
        id=PROFILE_ID,
        name="Test Profile",
        search=SearchConfig(queries=["q"], filters={}, poll=PollConfig()),
        seed_baselines=seed_baselines
        if seed_baselines is not None
        else [{"match": {}, "p25": 100, "p50": 150}],
        alerts=make_alerts_config() if alerts is _UNSET else alerts,
    )


def make_conn(tmp_path):
    return connect(tmp_path / "dealwatch.db")


def seed_listing(
    conn,
    item_id,
    *,
    price_cents=9000,
    spec_status="ok",
    bucket_key=BUCKET,
    spec=None,
    variation_id=None,
    item_web_url="https://ebay.com/itm/1",
    gone=False,
    seen_at=1000,
):
    raw = {
        "itemId": item_id,
        "title": "t",
        "price": {"value": f"{price_cents / 100:.2f}"},
    }
    record_sighting(
        conn,
        item_id,
        dict(
            profile_id=PROFILE_ID,
            title="t",
            variation_id=variation_id,
            item_web_url=item_web_url,
        ),
        dict(price_cents=price_cents, shipping_cents=None, total_cents=None,
             raw_json=json.dumps(raw)),
        seen_at,
    )
    store_spec(
        conn, item_id,
        SpecResult(spec=spec or {}, spec_status=spec_status, reject_rule_id=None,
                   bucket_key=bucket_key),
    )
    if gone:
        conn.execute("UPDATE listings SET gone_at = ? WHERE item_id = ?", (seen_at, item_id))


# ---------------------------------------------------------------------------
# resolve_webhook_url
# ---------------------------------------------------------------------------


def test_resolve_webhook_url_returns_none_when_alerts_not_configured():
    profile = make_profile(alerts=None)
    assert alerting.resolve_webhook_url(profile) is None


def test_resolve_webhook_url_returns_the_env_value_when_set(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    profile = make_profile()
    assert alerting.resolve_webhook_url(profile) == "https://discord.example/webhook"


def test_resolve_webhook_url_raises_when_env_var_is_unset(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_TEST", raising=False)
    profile = make_profile()
    with pytest.raises(ProfileCompileError):
        alerting.resolve_webhook_url(profile)


def test_resolve_webhook_url_raises_when_env_var_is_empty(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "")
    profile = make_profile()
    with pytest.raises(ProfileCompileError):
        alerting.resolve_webhook_url(profile)


# ---------------------------------------------------------------------------
# resolve_notifier_credentials (V0.9a)
# ---------------------------------------------------------------------------


def test_resolve_notifier_credentials_returns_empty_dict_without_an_alerts_block():
    profile = make_profile(alerts=None)
    assert alerting.resolve_notifier_credentials(profile) == {}


def test_resolve_notifier_credentials_returns_empty_dict_for_an_empty_notifiers_list():
    profile = make_profile(alerts=make_alerts_config(notifiers=[]))
    assert alerting.resolve_notifier_credentials(profile) == {}


def test_resolve_notifier_credentials_resolves_discord(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    profile = make_profile(alerts=make_alerts_config(notifiers=["discord"]))
    credentials = alerting.resolve_notifier_credentials(profile)
    assert credentials == {"discord": "https://discord.example/webhook"}


def test_resolve_notifier_credentials_resolves_pushover(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    profile = make_profile(alerts=make_alerts_config(notifiers=["pushover"]))
    credentials = alerting.resolve_notifier_credentials(profile)
    assert credentials == {"pushover": ("app-token", "user-key")}


def test_resolve_notifier_credentials_resolves_both_when_both_configured(monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    profile = make_profile(alerts=make_alerts_config(notifiers=["discord", "pushover"]))
    credentials = alerting.resolve_notifier_credentials(profile)
    assert credentials == {
        "discord": "https://discord.example/webhook",
        "pushover": ("app-token", "user-key"),
    }


def test_resolve_notifier_credentials_raises_when_pushover_app_token_missing(monkeypatch):
    monkeypatch.delenv("PUSHOVER_APP_TOKEN", raising=False)
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    profile = make_profile(alerts=make_alerts_config(notifiers=["pushover"]))
    with pytest.raises(ProfileCompileError):
        alerting.resolve_notifier_credentials(profile)


def test_resolve_notifier_credentials_raises_when_pushover_user_key_missing(monkeypatch):
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.delenv("PUSHOVER_USER_KEY", raising=False)
    profile = make_profile(alerts=make_alerts_config(notifiers=["pushover"]))
    with pytest.raises(ProfileCompileError):
        alerting.resolve_notifier_credentials(profile)


def test_invalid_notifier_name_is_a_profile_load_error():
    # schema.py's Literal["discord", "pushover"] type does this - not
    # resolve_notifier_credentials, and not a runtime ProfileCompileError.
    # A typo'd notifier name is a shape error, same family as a malformed
    # regex in a MatchRule, so it should fail exactly like every other
    # pydantic validation error on this model does.
    with pytest.raises(pydantic.ValidationError):
        make_alerts_config(notifiers=["discrod"])


# ---------------------------------------------------------------------------
# evaluate() - one test per gate, asserting the specific reason, not just
# "the list is empty"
# ---------------------------------------------------------------------------


def test_happy_path_survives_every_gate(tmp_path):
    # The positive control: a listing passing every gate must appear,
    # scored (gate 6) - not merely absent from every negative test below.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)  # ratio 90/100 = 0.90
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)

    assert [r.item_id for r in results] == ["item-1"]
    assert results[0].ratio_to_p25 == pytest.approx(0.90)


def test_gate1_skips_a_listing_marked_gone(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, gone=True)
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert results == []


def test_gate1_skips_an_item_id_that_does_not_exist(tmp_path):
    conn = make_conn(tmp_path)
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["never-seen"], now_ts=2000)
    assert results == []


def test_gate2_skips_rejected_spec_status(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, spec_status="rejected")
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert results == []


def test_gate2_allows_partial_spec_status(tmp_path):
    # partial must NOT be gated out - only rejected/not_target/pending/stale
    # never alert (design.md's dated entry).
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, spec_status="partial")
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert [r.item_id for r in results] == ["item-1"]


def test_gate3_skips_a_variation_listing_by_default(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "v1|999|456", price_cents=9000, variation_id="456")
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["v1|999|456"], now_ts=2000)
    assert results == []


def test_gate3_allows_a_variation_listing_when_include_variations_is_true(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "v1|999|456", price_cents=9000, variation_id="456")
    profile = make_profile(alerts=make_alerts_config(include_variations=True))
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["v1|999|456"], now_ts=2000)
    assert [r.item_id for r in results] == ["v1|999|456"]


def test_gate4_skips_a_listing_with_a_fully_unknown_bucket_key(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, bucket_key="?|?|?")
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert results == []


def test_gate4_skips_a_listing_with_a_partially_unknown_bucket_key(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, bucket_key="1|?|16")
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert results == []


def test_gate4_skips_a_listing_with_a_null_bucket_key(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, bucket_key=None)
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert results == []


def test_gate4_allows_a_complete_bucket_key(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, bucket_key="1|intel-10th|16")
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert [r.item_id for r in results] == ["item-1"]


def test_gate4_require_complete_bucket_false_lets_a_question_mark_bucket_through(tmp_path):
    # The escape hatch - a profile can explicitly opt out.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, bucket_key="1|?|16")
    profile = make_profile(alerts=make_alerts_config(require_complete_bucket=False))
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert [r.item_id for r in results] == ["item-1"]


def test_gate4_runs_before_scoring_not_merely_before_alerting(tmp_path, monkeypatch):
    # A test that only checks the outcome (results == []) would pass even if
    # this gate were misplaced AFTER scoring - the milestone's own warning.
    # Patching score_listing itself and asserting it was never called is
    # what actually proves the gate runs first.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000, bucket_key="1|?|16")
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    calls = []
    monkeypatch.setattr(
        alerting, "score_listing", lambda *a, **k: calls.append((a, k))
    )

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)

    assert results == []
    assert calls == []


def test_gate5_skips_a_listing_with_no_usable_price(tmp_path):
    conn = make_conn(tmp_path)
    # An auction row with no BIN at all: price_cents/total_cents both NULL.
    record_sighting(
        conn, "item-1",
        dict(profile_id=PROFILE_ID, title="t"),
        dict(price_cents=None, shipping_cents=None, total_cents=None,
             raw_json=json.dumps({"itemId": "item-1", "title": "t"})),
        1000,
    )
    store_spec(conn, "item-1", SpecResult(spec={}, spec_status="ok",
                                           reject_rule_id=None, bucket_key=BUCKET))
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert results == []


def test_gate6_skips_a_listing_priced_over_the_buying_ceiling(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=200000)  # $2000
    profile = make_profile(alerts=make_alerts_config(max_price_usd=1000.0))
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert results == []


def test_gate7_skips_a_listing_that_is_not_a_good_enough_deal(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=20000)  # ratio 200/100 = 2.0 > 1.0
    profile = make_profile()
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=2000)
    assert results == []


def test_gate8_skips_a_listing_still_in_cooldown(tmp_path):
    conn = make_conn(tmp_path)
    # price_cents=5000 is a huge (50%) drop from the prior alert's 10000 -
    # deliberately, so the realert_drop gate (8b) would NOT be what's
    # keeping this out. If the cooldown check (8a) were broken (e.g.
    # inverted or off-by-one), this item would incorrectly survive despite
    # the drop being more than enough - isolating the mechanism this test
    # is actually named for.
    seed_listing(conn, "item-1", price_cents=5000)
    profile = make_profile(alerts=make_alerts_config(cooldown_minutes=60))
    seeds = compile_seed_baselines(profile)
    record_alert(
        conn, "item-1", PROFILE_ID, sent_at=1000,
        dry_run=False, price_cents=10000, price_is_price_only=False,
        bucket_key=BUCKET, baseline_layer="seed", baseline_match="{}",
        baseline_n=None, baseline_p25_cents=10000, baseline_p50_cents=15000,
        ratio_to_p25=0.9, sanity_flagged=False, delivery_status="sent",
    )

    # 30 minutes later - well inside the 60-minute cooldown.
    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=1000 + 30 * 60)
    assert results == []


def test_gate8_cooldown_boundary_exactly_at_the_edge_passes(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9200)
    profile = make_profile(alerts=make_alerts_config(cooldown_minutes=60, realert_drop_pct=8))
    seeds = compile_seed_baselines(profile)
    # Last alert at price 10000; 9200 is exactly an 8% drop (the boundary
    # the realert_drop_pct gate itself is tested on below) - isolating this
    # test to the cooldown boundary alone.
    record_alert(
        conn, "item-1", PROFILE_ID, sent_at=1000,
        dry_run=False, price_cents=10000, price_is_price_only=False,
        bucket_key=BUCKET, baseline_layer="seed", baseline_match="{}",
        baseline_n=None, baseline_p25_cents=10000, baseline_p50_cents=15000,
        ratio_to_p25=1.0, sanity_flagged=False, delivery_status="sent",
    )

    # Exactly cooldown_minutes*60 seconds later - the comparison is strict
    # `<`, so exactly-at-the-edge must PASS, not skip.
    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=1000 + 60 * 60)
    assert [r.item_id for r in results] == ["item-1"]


def test_gate8_realert_drop_boundary_exactly_enough_drop_passes(tmp_path):
    conn = make_conn(tmp_path)
    # last_price_cents=10000, realert_drop_pct=8 -> threshold is exactly
    # 9200 (10000 * 92 / 100). 9200 must PASS.
    seed_listing(conn, "item-1", price_cents=9200)
    profile = make_profile(alerts=make_alerts_config(cooldown_minutes=0, realert_drop_pct=8))
    seeds = compile_seed_baselines(profile)
    record_alert(
        conn, "item-1", PROFILE_ID, sent_at=1000,
        dry_run=False, price_cents=10000, price_is_price_only=False,
        bucket_key=BUCKET, baseline_layer="seed", baseline_match="{}",
        baseline_n=None, baseline_p25_cents=10000, baseline_p50_cents=15000,
        ratio_to_p25=1.0, sanity_flagged=False, delivery_status="sent",
    )

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=1000 + 3600)
    assert [r.item_id for r in results] == ["item-1"]


def test_gate8_realert_drop_boundary_one_cent_short_skips(tmp_path):
    conn = make_conn(tmp_path)
    # 9201 is one cent short of the 9200 threshold - must skip.
    seed_listing(conn, "item-1", price_cents=9201)
    profile = make_profile(alerts=make_alerts_config(cooldown_minutes=0, realert_drop_pct=8))
    seeds = compile_seed_baselines(profile)
    record_alert(
        conn, "item-1", PROFILE_ID, sent_at=1000,
        dry_run=False, price_cents=10000, price_is_price_only=False,
        bucket_key=BUCKET, baseline_layer="seed", baseline_match="{}",
        baseline_n=None, baseline_p25_cents=10000, baseline_p50_cents=15000,
        ratio_to_p25=1.0, sanity_flagged=False, delivery_status="sent",
    )

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=1000 + 3600)
    assert results == []


def test_gate8_a_dry_run_prior_alert_still_gates_cooldown(tmp_path):
    # last_alert() ignores dry_run by design (storage/sqlite.py's
    # docstring) - a dry-run row must gate cooldown/re-alert exactly like a
    # real send would.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(alerts=make_alerts_config(cooldown_minutes=60))
    seeds = compile_seed_baselines(profile)
    record_alert(
        conn, "item-1", PROFILE_ID, sent_at=1000,
        dry_run=True, price_cents=9000, price_is_price_only=False,
        bucket_key=BUCKET, baseline_layer="seed", baseline_match="{}",
        baseline_n=None, baseline_p25_cents=10000, baseline_p50_cents=15000,
        ratio_to_p25=0.9, sanity_flagged=False, delivery_status="dry_run",
    )

    results = alerting.evaluate(conn, profile, seeds, ["item-1"], now_ts=1000 + 30 * 60)
    assert results == []


# ---------------------------------------------------------------------------
# max_per_cycle
# ---------------------------------------------------------------------------


def test_max_per_cycle_selects_the_best_ratios_not_an_arbitrary_slice(tmp_path):
    conn = make_conn(tmp_path)
    # Deliberately seeded out of ratio order, so a slice-in-item-order bug
    # would pick the wrong two.
    seed_listing(conn, "mid", price_cents=8000)     # ratio 0.80
    seed_listing(conn, "worst", price_cents=9900)    # ratio 0.99
    seed_listing(conn, "best", price_cents=5000)     # ratio 0.50
    profile = make_profile(alerts=make_alerts_config(max_per_cycle=2))
    seeds = compile_seed_baselines(profile)

    results = alerting.evaluate(
        conn, profile, seeds, ["mid", "worst", "best"], now_ts=2000
    )

    assert [r.item_id for r in results] == ["best", "mid"]


def test_max_per_cycle_logs_the_suppressed_count(tmp_path, caplog):
    import logging

    conn = make_conn(tmp_path)
    seed_listing(conn, "a", price_cents=8000)
    seed_listing(conn, "b", price_cents=8100)
    profile = make_profile(alerts=make_alerts_config(max_per_cycle=1))
    seeds = compile_seed_baselines(profile)

    with caplog.at_level(logging.INFO):
        alerting.evaluate(conn, profile, seeds, ["a", "b"], now_ts=2000)

    assert any("suppress" in record.message.lower() for record in caplog.records)


# ---------------------------------------------------------------------------
# run_alert_cycle - the driver. Mocks discord.send_alert directly (module
# patch), never a real HTTP call.
# ---------------------------------------------------------------------------


def test_run_alert_cycle_is_a_noop_without_an_alerts_block(tmp_path):
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(alerts=None)

    run(alerting.run_alert_cycle(conn, profile, [], ["item-1"], now_ts=2000))

    assert conn.execute("SELECT COUNT(*) FROM alerts").fetchone()[0] == 0


def test_dry_run_writes_a_row_and_posts_nothing(tmp_path, monkeypatch):
    # The webhook env var is still required even in dry_run - Collector
    # resolves it once at startup regardless of dry_run (design.md's dated
    # entry: the whole point is finding out the env var is missing on day
    # one, not the morning dry_run flips to false) - so this test sets it
    # even though send_alert is never actually called.
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(alerts=make_alerts_config(dry_run=True))
    seeds = compile_seed_baselines(profile)

    calls = []

    async def fake_send_alert(*args, **kwargs):
        calls.append((args, kwargs))
        return "sent"

    monkeypatch.setattr(alerting.discord, "send_alert", fake_send_alert)

    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=2000))

    assert calls == []
    row = conn.execute("SELECT * FROM alerts WHERE item_id = 'item-1'").fetchone()
    assert row is not None
    assert row["dry_run"] == 1
    assert row["delivery_status"] == "dry_run"


def test_the_dry_run_guard_property_a_dry_run_cycle_then_a_live_cycle_posts_nothing(
    tmp_path, monkeypatch
):
    # This is the mechanism that prevents an alert storm on the ~1,000
    # already-active listings the first time dry_run flips to false
    # (design.md's dated entry) - tested as its own named case, not folded
    # into the dry-run test above.
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(alerts=make_alerts_config(dry_run=True, cooldown_minutes=60))
    seeds = compile_seed_baselines(profile)

    calls = []

    async def fake_send_alert(*args, **kwargs):
        calls.append((args, kwargs))
        return "sent"

    monkeypatch.setattr(alerting.discord, "send_alert", fake_send_alert)

    # Day 1: dry run.
    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=1000))
    assert calls == []

    # Day 2: flip to live, same item, same price, well within cooldown.
    profile.alerts.dry_run = False
    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=1000 + 3600))

    assert calls == []  # still zero posts - cooldown against the dry-run row


def test_live_send_failure_still_writes_an_alert_row(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(alerts=make_alerts_config(dry_run=False))
    seeds = compile_seed_baselines(profile)

    async def fake_send_alert(*args, **kwargs):
        return "failed"

    monkeypatch.setattr(alerting.discord, "send_alert", fake_send_alert)

    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=2000))

    row = conn.execute("SELECT * FROM alerts WHERE item_id = 'item-1'").fetchone()
    assert row is not None
    assert row["dry_run"] == 0
    assert row["delivery_status"] == "failed"


# ---------------------------------------------------------------------------
# run_alert_cycle - multi-notifier (V0.9a). Mocks discord.send_alert AND
# pushover.send_alert directly (module patches, same convention as above).
# ---------------------------------------------------------------------------


def _rows_for(conn, item_id="item-1"):
    return conn.execute(
        "SELECT notifier, delivery_status FROM alerts WHERE item_id = ? "
        "ORDER BY notifier",
        (item_id,),
    ).fetchall()


def test_both_notifiers_configured_writes_two_rows(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(
        alerts=make_alerts_config(dry_run=False, notifiers=["discord", "pushover"])
    )
    seeds = compile_seed_baselines(profile)

    async def fake_send_alert(*args, **kwargs):
        return "sent"

    monkeypatch.setattr(alerting.discord, "send_alert", fake_send_alert)
    monkeypatch.setattr(alerting.pushover, "send_alert", fake_send_alert)

    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=2000))

    rows = _rows_for(conn)
    assert [dict(r) for r in rows] == [
        {"notifier": "discord", "delivery_status": "sent"},
        {"notifier": "pushover", "delivery_status": "sent"},
    ]


def test_discord_raising_does_not_prevent_pushover(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(
        alerts=make_alerts_config(dry_run=False, notifiers=["discord", "pushover"])
    )
    seeds = compile_seed_baselines(profile)

    pushover_calls = []

    async def raising_discord(*args, **kwargs):
        raise RuntimeError("discord blew up")

    async def fake_pushover(*args, **kwargs):
        pushover_calls.append((args, kwargs))
        return "sent"

    monkeypatch.setattr(alerting.discord, "send_alert", raising_discord)
    monkeypatch.setattr(alerting.pushover, "send_alert", fake_pushover)

    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=2000))

    assert len(pushover_calls) == 1  # attempted despite discord raising
    rows = {r["notifier"]: r["delivery_status"] for r in _rows_for(conn)}
    assert rows == {"discord": "failed", "pushover": "sent"}


def test_pushover_raising_does_not_prevent_discord(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(
        alerts=make_alerts_config(dry_run=False, notifiers=["discord", "pushover"])
    )
    seeds = compile_seed_baselines(profile)

    discord_calls = []

    async def fake_discord(*args, **kwargs):
        discord_calls.append((args, kwargs))
        return "sent"

    async def raising_pushover(*args, **kwargs):
        raise RuntimeError("pushover blew up")

    monkeypatch.setattr(alerting.discord, "send_alert", fake_discord)
    monkeypatch.setattr(alerting.pushover, "send_alert", raising_pushover)

    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=2000))

    assert len(discord_calls) == 1  # attempted despite pushover raising
    rows = {r["notifier"]: r["delivery_status"] for r in _rows_for(conn)}
    assert rows == {"discord": "sent", "pushover": "failed"}


def test_notifiers_discord_only_writes_one_row_and_never_calls_pushover(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(alerts=make_alerts_config(dry_run=False, notifiers=["discord"]))
    seeds = compile_seed_baselines(profile)

    pushover_calls = []

    async def fake_discord(*args, **kwargs):
        return "sent"

    async def fake_pushover(*args, **kwargs):
        pushover_calls.append((args, kwargs))
        return "sent"

    monkeypatch.setattr(alerting.discord, "send_alert", fake_discord)
    monkeypatch.setattr(alerting.pushover, "send_alert", fake_pushover)

    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=2000))

    assert pushover_calls == []
    rows = _rows_for(conn)
    assert len(rows) == 1
    assert rows[0]["notifier"] == "discord"


def test_notifiers_empty_list_sends_nothing_but_still_writes_a_row(tmp_path, monkeypatch):
    # Empty notifiers is "evaluate and record, tell no one" - not "do
    # nothing" (design.md's dated entry): without a row, evaluate()'s
    # cooldown/re-alert gate has nothing to dedup against next cycle.
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(alerts=make_alerts_config(dry_run=False, notifiers=[]))
    seeds = compile_seed_baselines(profile)

    discord_calls = []
    pushover_calls = []
    monkeypatch.setattr(
        alerting.discord, "send_alert",
        lambda *a, **k: discord_calls.append((a, k)),
    )
    monkeypatch.setattr(
        alerting.pushover, "send_alert",
        lambda *a, **k: pushover_calls.append((a, k)),
    )

    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=2000))

    assert discord_calls == []
    assert pushover_calls == []
    rows = _rows_for(conn)
    assert len(rows) == 1
    assert rows[0]["notifier"] == "none"
    assert rows[0]["delivery_status"] == "skipped"


def test_dry_run_writes_one_dry_run_row_per_configured_notifier(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_TEST", "https://discord.example/webhook")
    monkeypatch.setenv("PUSHOVER_APP_TOKEN", "app-token")
    monkeypatch.setenv("PUSHOVER_USER_KEY", "user-key")
    conn = make_conn(tmp_path)
    seed_listing(conn, "item-1", price_cents=9000)
    profile = make_profile(
        alerts=make_alerts_config(dry_run=True, notifiers=["discord", "pushover"])
    )
    seeds = compile_seed_baselines(profile)

    calls = []
    monkeypatch.setattr(
        alerting.discord, "send_alert", lambda *a, **k: calls.append((a, k))
    )
    monkeypatch.setattr(
        alerting.pushover, "send_alert", lambda *a, **k: calls.append((a, k))
    )

    run(alerting.run_alert_cycle(conn, profile, seeds, ["item-1"], now_ts=2000))

    assert calls == []  # dry_run never calls a notifier's send_alert
    rows = {r["notifier"]: r["delivery_status"] for r in _rows_for(conn)}
    assert rows == {"discord": "dry_run", "pushover": "dry_run"}
