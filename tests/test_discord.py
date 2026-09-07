"""Tests for dealwatch.notify.discord (V0.9, design.md's dated entry).

build_embed() is pure and tested directly with no I/O at all. send_alert()
is tested against httpx.MockTransport (same convention as
test_ebay_browse.py) - no test here makes a real HTTP call.
"""

import asyncio

import httpx

from dealwatch.engine.scoring import BASELINE_LAYER_COMPUTED, BASELINE_LAYER_SEED, ScoreResult
from dealwatch.normalize.schema import AlertsConfig, AlertTrigger
from dealwatch.notify import discord


def run(coro):
    return asyncio.run(coro)


def make_result(**overrides) -> ScoreResult:
    defaults = dict(
        item_id="item-1",
        bucket_key="1|intel-10th|16",
        price_cents=9000,
        price_is_price_only=False,
        baseline_layer=BASELINE_LAYER_SEED,
        baseline_match="{}",
        baseline_n=None,
        baseline_p25_cents=10000,
        baseline_p50_cents=15000,
        ratio_to_p25=0.9,
        ratio_to_p50=0.6,
        sanity_flagged=False,
        item_web_url="https://ebay.com/itm/1",
    )
    defaults.update(overrides)
    return ScoreResult(**defaults)


def make_alerts_config(**overrides) -> AlertsConfig:
    defaults = dict(
        webhook_env="DISCORD_WEBHOOK_TEST",
        dry_run=True,
        trigger=AlertTrigger(),
        max_price_usd=1000.0,
        title_template="Gen {generation} {cpu_family} {ram_tier}GB",
        fields=["generation", "cpu_family", "ram_tier"],
    )
    defaults.update(overrides)
    return AlertsConfig(**defaults)


# ---------------------------------------------------------------------------
# build_embed - pure, no I/O
# ---------------------------------------------------------------------------


def test_title_renders_from_the_template_and_spec():
    embed = discord.build_embed(
        make_result(), {"generation": "2", "cpu_family": "intel-11th", "ram_tier": "16"},
        make_alerts_config(),
    )
    assert embed["title"] == "Gen 2 intel-11th 16GB"


def test_title_renders_unknown_for_a_field_missing_from_spec():
    # A profile hunting a different target could reference a spec key this
    # listing's spec never produced - a partial listing, a legitimate
    # runtime state, must render "unknown", never raise.
    embed = discord.build_embed(
        make_result(), {"generation": "2"}, make_alerts_config(),
    )
    assert embed["title"] == "Gen 2 unknown unknownGB"


def test_title_renders_unknown_for_a_field_present_but_none():
    embed = discord.build_embed(
        make_result(),
        {"generation": "2", "cpu_family": None, "ram_tier": "16"},
        make_alerts_config(),
    )
    assert embed["title"] == "Gen 2 unknown 16GB"


def test_fields_render_unknown_for_missing_or_none_values():
    embed = discord.build_embed(
        make_result(),
        {"generation": "2", "cpu_family": None},
        make_alerts_config(fields=["generation", "cpu_family", "ram_tier"]),
    )
    rendered = {f["name"]: f["value"] for f in embed["fields"]}
    assert rendered == {"generation": "2", "cpu_family": "unknown", "ram_tier": "unknown"}


def test_computed_baseline_footer_shows_n():
    embed = discord.build_embed(
        make_result(baseline_layer=BASELINE_LAYER_COMPUTED, baseline_n=24),
        {}, make_alerts_config(),
    )
    assert "computed baseline" in embed["footer"]["text"]
    assert "n=24" in embed["footer"]["text"]


def test_seed_baseline_footer_is_visually_distinct():
    embed = discord.build_embed(
        make_result(baseline_layer=BASELINE_LAYER_SEED), {}, make_alerts_config(),
    )
    assert "SEED ESTIMATE" in embed["footer"]["text"]
    assert "computed" not in embed["footer"]["text"].lower()


def test_price_is_price_only_is_called_out_in_the_description():
    embed = discord.build_embed(
        make_result(price_is_price_only=True), {}, make_alerts_config(),
    )
    assert "shipping unknown" in embed["description"]


def test_price_is_price_only_false_omits_the_shipping_caveat():
    embed = discord.build_embed(
        make_result(price_is_price_only=False), {}, make_alerts_config(),
    )
    assert "shipping unknown" not in embed["description"]


def test_sanity_flagged_true_is_rendered():
    embed = discord.build_embed(
        make_result(sanity_flagged=True), {}, make_alerts_config(),
    )
    assert "sanity floor" in embed["description"].lower()


def test_sanity_flagged_false_is_not_rendered():
    embed = discord.build_embed(
        make_result(sanity_flagged=False), {}, make_alerts_config(),
    )
    assert "sanity floor" not in embed["description"].lower()


def test_embed_url_is_the_listing_url():
    embed = discord.build_embed(
        make_result(item_web_url="https://ebay.com/itm/12345"), {}, make_alerts_config(),
    )
    assert embed["url"] == "https://ebay.com/itm/12345"


# ---------------------------------------------------------------------------
# send_alert - httpx.MockTransport, no real HTTP call
# ---------------------------------------------------------------------------


def test_send_alert_returns_sent_on_200(tmp_path):
    run(_send_alert_returns_sent_on_200())


async def _send_alert_returns_sent_on_200():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await discord.send_alert(
            "https://discord.example/webhook", make_result(), {}, make_alerts_config(),
            client=client,
        )
    finally:
        await client.aclose()

    assert status == "sent"
    assert len(calls) == 1


def test_send_alert_retries_once_on_429_then_succeeds(monkeypatch):
    run(_send_alert_retries_once_on_429_then_succeeds(monkeypatch))


async def _send_alert_retries_once_on_429_then_succeeds(monkeypatch):
    calls = []

    async def sleep_stub(_seconds):
        pass  # don't actually sleep in a test

    monkeypatch.setattr(discord.asyncio, "sleep", sleep_stub)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await discord.send_alert(
            "https://discord.example/webhook", make_result(), {}, make_alerts_config(),
            client=client,
        )
    finally:
        await client.aclose()

    assert status == "sent"
    assert len(calls) == 2  # exactly one retry, bounded


def test_send_alert_gives_up_after_two_attempts_still_429(monkeypatch):
    run(_send_alert_gives_up_after_two_attempts_still_429(monkeypatch))


async def _send_alert_gives_up_after_two_attempts_still_429(monkeypatch):
    calls = []

    async def sleep_stub(_seconds):
        pass

    monkeypatch.setattr(discord.asyncio, "sleep", sleep_stub)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json={"retry_after": 0.01})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await discord.send_alert(
            "https://discord.example/webhook", make_result(), {}, make_alerts_config(),
            client=client,
        )
    finally:
        await client.aclose()

    assert status == "failed"
    assert len(calls) == 2  # bounded at 2, not an unbounded retry loop


def test_send_alert_5xx_fails_immediately_no_retry():
    run(_send_alert_5xx_fails_immediately_no_retry())


async def _send_alert_5xx_fails_immediately_no_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(500, text="internal error")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await discord.send_alert(
            "https://discord.example/webhook", make_result(), {}, make_alerts_config(),
            client=client,
        )
    finally:
        await client.aclose()

    assert status == "failed"
    assert len(calls) == 1  # unlike 429, a 5xx is not retried


def test_send_alert_connection_error_returns_failed_without_raising():
    run(_send_alert_connection_error_returns_failed_without_raising())


async def _send_alert_connection_error_returns_failed_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await discord.send_alert(
            "https://discord.example/webhook", make_result(), {}, make_alerts_config(),
            client=client,
        )
    finally:
        await client.aclose()

    assert status == "failed"  # never raises into the caller
