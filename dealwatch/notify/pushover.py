"""Pushover notifier (V0.9a, design.md's dated entry).

Renders the same ScoreResult + spec dict + AlertsConfig that
notify/discord.py does, but as plain-text message lines instead of an
embed - Pushover's API has no field/embed structure, just `title` and
`message`. Same genericity rule as discord.py: no laptop-specific
knowledge, no hardcoded field names. Everything rendered comes from
alerts.title_template / alerts.fields.

POSTs to https://api.pushover.net/1/messages.json, form-encoded, using
credentials the caller already resolved
(dealwatch.engine.alerting.resolve_notifier_credentials) - this module
never reads the environment itself, same division of labor as discord.py.
"""

import asyncio
import logging

import httpx

from dealwatch.engine.scoring import BASELINE_LAYER_COMPUTED, ScoreResult
from dealwatch.normalize.schema import AlertsConfig
from dealwatch.notify._shared import render_field_value, render_title

logger = logging.getLogger(__name__)

_API_URL = "https://api.pushover.net/1/messages.json"

# Same bounded-retry shape as discord.py, same reasoning: an outage must
# have a bounded cost, never an unbounded retry loop stalling the collector.
_MAX_ATTEMPTS = 2
_DEFAULT_RETRY_AFTER_SECONDS = 1.0

# Pushover's own hard cap on the `message` field. Truncating defensively
# here means a long title_template/fields combination degrades to a
# clipped-but-delivered message instead of a 4xx from Pushover's API.
_MAX_MESSAGE_LENGTH = 1024
_TRUNCATION_SUFFIX = "... (truncated)"


def build_message(result: ScoreResult, spec: dict, alerts_cfg: AlertsConfig) -> str:
    """Pure - no I/O, so it's directly testable without a mock transport.

    The baseline-layer line is the single most important thing on the card
    (design.md's V0.9 dated entry, carried forward here) - in Discord's
    embed it's a footer; here, with no embed structure to put it in, it's
    simply the last line of the message body. Built and appended AFTER
    truncation is computed for the rest of the body, so a long spec/fields
    combination can never crowd it out - losing this line is worse than
    losing message text, since it's what tells the reader "seed guess" vs.
    "24 observed sales" rather than a plausible-looking number either way.
    """
    price_line = f"${result.price_cents / 100:.2f}"
    if result.price_is_price_only:
        price_line += " (price only - shipping unknown, not free)"

    lines = [
        price_line,
        f"ratio to p25: {result.ratio_to_p25:.2f}  ·  ratio to p50: {result.ratio_to_p50:.2f}",
    ]
    if result.sanity_flagged:
        lines.append("BELOW SANITY FLOOR - verify before buying")

    for name in alerts_cfg.fields:
        lines.append(f"{name}: {render_field_value(spec, name)}")

    if result.item_web_url:
        lines.append(result.item_web_url)

    if result.baseline_layer == BASELINE_LAYER_COMPUTED:
        baseline_line = f"computed baseline - n={result.baseline_n}"
    else:
        baseline_line = "SEED ESTIMATE - no observed data"

    body = "\n".join(lines)
    budget = _MAX_MESSAGE_LENGTH - len(baseline_line) - 1  # 1 for the join newline
    if len(body) > budget:
        body = body[: budget - len(_TRUNCATION_SUFFIX)] + _TRUNCATION_SUFFIX

    return f"{body}\n{baseline_line}"


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        body = response.json()
        return float(body.get("retry_after", _DEFAULT_RETRY_AFTER_SECONDS))
    except (ValueError, TypeError, KeyError):
        return _DEFAULT_RETRY_AFTER_SECONDS


async def send_alert(
    credentials: tuple[str, str],
    result: ScoreResult,
    spec: dict,
    alerts_cfg: AlertsConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """POST one deal alert to Pushover. Returns 'sent' or 'failed' - NEVER
    raises into the caller, same contract as discord.send_alert. `credentials`
    is (app_token, user_key), the shape
    engine.alerting.resolve_notifier_credentials produces for the
    "pushover" entry.

    `client` is optional and test-only, same convention as discord.py.
    """
    app_token, user_key = credentials
    title = render_title(alerts_cfg.title_template, spec)
    message = build_message(result, spec, alerts_cfg)
    payload = {
        "token": app_token,
        "user": user_key,
        "title": title,
        "message": message,
        "url": result.item_web_url or "",
        "url_title": "View listing",
    }

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.post(_API_URL, data=payload)
            except httpx.HTTPError:
                logger.exception(
                    "pushover request failed for item_id=%s", result.item_id
                )
                return "failed"

            if response.status_code == 429:
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_retry_after_seconds(response))
                    continue
                logger.warning(
                    "pushover still rate-limited after %d attempts for "
                    "item_id=%s; giving up",
                    attempt,
                    result.item_id,
                )
                return "failed"

            if response.status_code >= 400:
                logger.warning(
                    "pushover returned %s for item_id=%s: %s",
                    response.status_code,
                    result.item_id,
                    response.text,
                )
                return "failed"

            return "sent"

        return "failed"  # unreachable - the loop always returns
    finally:
        if owns_client:
            await client.aclose()
