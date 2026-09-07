"""Discord webhook notifier (V0.9, design.md's dated entry).

Builds one embed from a ScoreResult + a listing's spec dict + the profile's
AlertsConfig, and POSTs it to a webhook URL the caller already resolved
(dealwatch.engine.alerting.resolve_webhook_url) - this module never reads
the environment itself.

This module must not contain any laptop-specific knowledge: no hardcoded
"Generation"/"CPU"/"RAM" labels, no field names in Python. Everything
rendered comes from alerts.title_template and alerts.fields, so a profile
hunting M.2 drives needs only a YAML change here, never a code change.
"""

import asyncio
import logging

import httpx

from dealwatch.engine.scoring import BASELINE_LAYER_COMPUTED, ScoreResult
from dealwatch.normalize.schema import AlertsConfig

logger = logging.getLogger(__name__)

# "Bounded retry - at most 2 attempts, then give up" (design.md's dated
# entry): a webhook outage must have a bounded cost, never an unbounded
# retry loop stalling the collector.
_MAX_ATTEMPTS = 2
_DEFAULT_RETRY_AFTER_SECONDS = 1.0


class _RenderSpec(dict):
    """A spec dict for str.format_map() where a missing key, OR a key whose
    value is None (a legitimate runtime state - see AlertsConfig's
    docstring), both render as "unknown" rather than raising or printing
    "None"."""

    def __missing__(self, key: str) -> str:
        return "unknown"

    def __getitem__(self, key: str):
        value = super().__getitem__(key)
        return "unknown" if value is None else value


def _render_title(template: str, spec: dict) -> str:
    return template.format_map(_RenderSpec(spec))


def _render_field_value(spec: dict, name: str) -> str:
    value = spec.get(name)
    return "unknown" if value is None else str(value)


def build_embed(result: ScoreResult, spec: dict, alerts_cfg: AlertsConfig) -> dict:
    """Pure - no I/O, so it's directly testable without a mock transport."""
    fields = [
        {"name": name, "value": _render_field_value(spec, name), "inline": True}
        for name in alerts_cfg.fields
    ]

    # The baseline layer is the single most important thing on the card
    # (design.md's dated entry) - right now only two buckets have computed
    # baselines, every other bucket alerts against a hand-authored guess,
    # and that distinction must be visually obvious, not a field someone
    # has to notice.
    if result.baseline_layer == BASELINE_LAYER_COMPUTED:
        footer_text = f"computed baseline · n={result.baseline_n}"
    else:
        footer_text = "SEED ESTIMATE · no observed data"

    price_line = f"${result.price_cents / 100:.2f}"
    if result.price_is_price_only:
        price_line += " (price only - shipping unknown, not free)"

    description_lines = [
        price_line,
        f"ratio to p25: {result.ratio_to_p25:.2f}  ·  ratio to p50: {result.ratio_to_p50:.2f}",
    ]
    if result.sanity_flagged:
        description_lines.append("**below sanity floor** - verify before buying")

    return {
        "title": _render_title(alerts_cfg.title_template, spec),
        "url": result.item_web_url,
        "description": "\n".join(description_lines),
        "fields": fields,
        "footer": {"text": footer_text},
    }


def _retry_after_seconds(response: httpx.Response) -> float:
    try:
        body = response.json()
        return float(body.get("retry_after", _DEFAULT_RETRY_AFTER_SECONDS))
    except (ValueError, TypeError, KeyError):
        return _DEFAULT_RETRY_AFTER_SECONDS


async def send_alert(
    webhook_url: str,
    result: ScoreResult,
    spec: dict,
    alerts_cfg: AlertsConfig,
    *,
    client: httpx.AsyncClient | None = None,
) -> str:
    """POST one deal alert to Discord. Returns 'sent' or 'failed' - NEVER
    raises into the caller. The collector's job is to not lose data; a
    Discord outage, a 5xx, or a malformed embed must not stall a poll
    cycle or a sighting write (design.md's dated entry).

    `client` is optional and test-only (mirrors EbayBrowseProvider's own
    convention) - by default this owns a short-lived AsyncClient for the
    single POST and closes it, since alerts are rare (max_per_cycle
    defaults to 10, typically far fewer per cycle in practice) and this
    module deliberately has no persistent-connection lifecycle to manage
    (no pluggable transports, one Discord module - CLAUDE.md).
    """
    embed = build_embed(result, spec, alerts_cfg)
    payload = {"embeds": [embed]}

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient()
    try:
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                response = await client.post(webhook_url, json=payload)
            except httpx.HTTPError:
                logger.exception(
                    "discord webhook request failed for item_id=%s", result.item_id
                )
                return "failed"

            if response.status_code == 429:
                if attempt < _MAX_ATTEMPTS:
                    await asyncio.sleep(_retry_after_seconds(response))
                    continue
                logger.warning(
                    "discord webhook still rate-limited after %d attempts for "
                    "item_id=%s; giving up",
                    attempt,
                    result.item_id,
                )
                return "failed"

            if response.status_code >= 400:
                logger.warning(
                    "discord webhook returned %s for item_id=%s: %s",
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
