"""Tests for dealwatch.notify.pushover (V0.9a, design.md's dated entry).

build_message() is pure and tested directly with no I/O at all. send_alert()
is tested against httpx.MockTransport, same convention as test_discord.py -
no test here makes a real HTTP call.
"""

import asyncio

import httpx

from dealwatch.engine.scoring import BASELINE_LAYER_COMPUTED, BASELINE_LAYER_SEED, ScoreResult
from dealwatch.normalize.schema import AlertsConfig, AlertTrigger
from dealwatch.notify import pushover


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
# build_message - pure, no I/O
# ---------------------------------------------------------------------------


def test_message_includes_price():
    message = pushover.build_message(make_result(price_cents=9050), {}, make_alerts_config())
    assert "$90.50" in message


def test_price_is_price_only_is_called_out():
    message = pushover.build_message(
        make_result(price_is_price_only=True), {}, make_alerts_config()
    )
    assert "shipping unknown" in message


def test_price_is_price_only_false_omits_the_caveat():
    message = pushover.build_message(
        make_result(price_is_price_only=False), {}, make_alerts_config()
    )
    assert "shipping unknown" not in message


def test_sanity_flagged_true_is_rendered():
    message = pushover.build_message(make_result(sanity_flagged=True), {}, make_alerts_config())
    assert "sanity floor" in message.lower()


def test_sanity_flagged_false_is_not_rendered():
    message = pushover.build_message(make_result(sanity_flagged=False), {}, make_alerts_config())
    assert "sanity floor" not in message.lower()


def test_fields_render_unknown_for_missing_or_none_values():
    message = pushover.build_message(
        make_result(),
        {"generation": "2", "cpu_family": None},
        make_alerts_config(fields=["generation", "cpu_family", "ram_tier"]),
    )
    assert "generation: 2" in message
    assert "cpu_family: unknown" in message
    assert "ram_tier: unknown" in message


def test_message_includes_the_listing_url():
    message = pushover.build_message(
        make_result(item_web_url="https://ebay.com/itm/12345"), {}, make_alerts_config()
    )
    assert "https://ebay.com/itm/12345" in message


def test_computed_baseline_line_shows_n():
    message = pushover.build_message(
        make_result(baseline_layer=BASELINE_LAYER_COMPUTED, baseline_n=24),
        {},
        make_alerts_config(),
    )
    assert "computed baseline" in message
    assert "n=24" in message


def test_seed_baseline_line_is_visually_distinct():
    message = pushover.build_message(
        make_result(baseline_layer=BASELINE_LAYER_SEED), {}, make_alerts_config()
    )
    assert "SEED ESTIMATE" in message
    assert "computed" not in message.lower()


def test_baseline_line_is_the_last_line_and_survives_truncation():
    # The single most important thing on the card (design.md's dated
    # entry, carried forward from Discord's footer) must not get crowded
    # out by a long fields list - it's computed and appended AFTER the
    # truncation budget is applied to everything else.
    long_fields = [f"field_{i}" for i in range(200)]
    spec = {name: "x" * 20 for name in long_fields}
    message = pushover.build_message(
        make_result(baseline_layer=BASELINE_LAYER_COMPUTED, baseline_n=24),
        spec,
        make_alerts_config(fields=long_fields),
    )
    assert len(message) <= 1024
    assert message.endswith("computed baseline - n=24")


def test_message_never_exceeds_pushovers_1024_char_cap():
    long_fields = [f"field_{i}" for i in range(500)]
    spec = {name: "x" * 50 for name in long_fields}
    message = pushover.build_message(
        make_result(), spec, make_alerts_config(fields=long_fields)
    )
    assert len(message) <= 1024


# ---------------------------------------------------------------------------
# send_alert - httpx.MockTransport, no real HTTP call
# ---------------------------------------------------------------------------

_CREDENTIALS = ("app-token-123", "user-key-456")


def test_send_alert_returns_sent_on_200():
    run(_send_alert_returns_sent_on_200())


async def _send_alert_returns_sent_on_200():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"status": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await pushover.send_alert(
            _CREDENTIALS, make_result(), {}, make_alerts_config(), client=client
        )
    finally:
        await client.aclose()

    assert status == "sent"
    assert len(calls) == 1
    sent_body = calls[0].content.decode()
    assert "app-token-123" in sent_body
    assert "user-key-456" in sent_body


def test_send_alert_retries_once_on_429_then_succeeds(monkeypatch):
    run(_send_alert_retries_once_on_429_then_succeeds(monkeypatch))


async def _send_alert_retries_once_on_429_then_succeeds(monkeypatch):
    calls = []

    async def sleep_stub(_seconds):
        pass

    monkeypatch.setattr(pushover.asyncio, "sleep", sleep_stub)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"retry_after": 0.01})
        return httpx.Response(200, json={"status": 1})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await pushover.send_alert(
            _CREDENTIALS, make_result(), {}, make_alerts_config(), client=client
        )
    finally:
        await client.aclose()

    assert status == "sent"
    assert len(calls) == 2


def test_send_alert_gives_up_after_two_attempts_still_429(monkeypatch):
    run(_send_alert_gives_up_after_two_attempts_still_429(monkeypatch))


async def _send_alert_gives_up_after_two_attempts_still_429(monkeypatch):
    calls = []

    async def sleep_stub(_seconds):
        pass

    monkeypatch.setattr(pushover.asyncio, "sleep", sleep_stub)

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(429, json={"retry_after": 0.01})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await pushover.send_alert(
            _CREDENTIALS, make_result(), {}, make_alerts_config(), client=client
        )
    finally:
        await client.aclose()

    assert status == "failed"
    assert len(calls) == 2


def test_send_alert_4xx_fails_immediately_no_retry():
    run(_send_alert_4xx_fails_immediately_no_retry())


async def _send_alert_4xx_fails_immediately_no_retry():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(400, json={"status": 0, "errors": ["invalid token"]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await pushover.send_alert(
            _CREDENTIALS, make_result(), {}, make_alerts_config(), client=client
        )
    finally:
        await client.aclose()

    assert status == "failed"
    assert len(calls) == 1


def test_send_alert_connection_error_returns_failed_without_raising():
    run(_send_alert_connection_error_returns_failed_without_raising())


async def _send_alert_connection_error_returns_failed_without_raising():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    try:
        status = await pushover.send_alert(
            _CREDENTIALS, make_result(), {}, make_alerts_config(), client=client
        )
    finally:
        await client.aclose()

    assert status == "failed"
