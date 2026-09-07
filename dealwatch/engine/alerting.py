"""Alert evaluation and delivery (V0.9, design.md's dated entry): decides
which active listings deserve a Discord alert this cycle, and drives
delivery + persistence for the survivors.

evaluate() is the decision logic and is pure aside from reading conn - no
network, no writes. run_alert_cycle() is the driver: it calls evaluate(),
then posts (or skips posting, in dry_run) and writes exactly one `alerts`
row per survivor, dry-run or not (storage/sqlite.py's last_alert()
docstring explains why dry-run rows are written at all).

No buyability/suppression rules here (soldered RAM, minimum specs) - V0.9a,
deliberately deferred (design.md §5.7's scope-change entry). No scores
table - scores are recomputable from observations + baselines; only alerts
(a real delivery decision, not a derived number) get persisted.
"""

import json
import logging
import os

import httpx

from dealwatch.engine.baselines import select_price
from dealwatch.engine.scoring import CompiledSeedBaseline, ScoreResult, score_listing
from dealwatch.normalize.engine import OK, PARTIAL, ProfileCompileError
from dealwatch.normalize.schema import Profile
from dealwatch.notify import discord
from dealwatch.storage.sqlite import get_latest_observation, last_alert, record_alert

logger = logging.getLogger(__name__)

_ALERT_CANDIDATE_LISTING = """
    SELECT item_id, profile_id, gone_at, spec_status, variation_id,
           bucket_key, spec_json, item_web_url
    FROM listings
    WHERE item_id = ? AND profile_id = ?
"""


def resolve_webhook_url(profile: Profile) -> str | None:
    """None if profile.alerts is not set (alerting disabled for this
    profile - Profile itself keeps `alerts` optional). If it IS set, the
    named env var being unset or empty is a startup error
    (ProfileCompileError, same family as an invalid regex or an
    unresolvable bucket_key field) - not a silent no-op discovered at 2am
    when nothing ever posts.

    Called once at startup (Collector.__init__, alongside compile_profile/
    compile_seed_baselines) AND once per run_alert_cycle call below - the
    second call is not re-validating out of paranoia, it's how
    run_alert_cycle gets the URL without needing its own extra parameter;
    re-reading one already-set environment variable is a dict lookup, not
    the kind of per-cycle cost compile_profile/compile_seed_baselines are
    worth avoiding by compiling once.
    """
    if profile.alerts is None:
        return None
    value = os.environ.get(profile.alerts.webhook_env)
    if not value:
        raise ProfileCompileError(
            f"alerts.webhook_env names {profile.alerts.webhook_env!r}, but "
            "it is unset or empty in the environment"
        )
    return value


def _passes_realert_drop(price_cents: int, last_price_cents: int, realert_drop_pct: int) -> bool:
    """price_cents is at least realert_drop_pct % below last_price_cents.
    Cross-multiplied, integer cents only - no float division, no rounding
    behavior to get subtly wrong right at the boundary (same reasoning as
    engine/scoring.py's sanity_flagged check)."""
    return price_cents * 100 <= last_price_cents * (100 - realert_drop_pct)


def evaluate(
    conn,
    profile: Profile,
    compiled_seeds: list[CompiledSeedBaseline],
    item_ids: list[str],
    now_ts: int,
) -> list[ScoreResult]:
    """Which of item_ids should alert this cycle, best deals first, capped
    at alerts.max_per_cycle. Each item is checked against every gate below,
    in order, short-circuiting on the first failure - see this module's
    tests for one case per gate, asserting the specific reason a listing
    was excluded, not merely that the result list is empty.

    profile.alerts must not be None - callers (run_alert_cycle) are
    responsible for that check, since "no alerts configured" is a caller
    decision, not a per-item gate.
    """
    alerts_cfg = profile.alerts
    survivors: list[ScoreResult] = []

    for item_id in item_ids:
        row = conn.execute(_ALERT_CANDIDATE_LISTING, (item_id, profile.id)).fetchone()
        if row is None or row["gone_at"] is not None:
            continue  # gate 1: must exist, be active, belong to this profile

        if row["spec_status"] not in (OK, PARTIAL):
            continue  # gate 2: rejected/not_target/pending/stale never alert

        if row["variation_id"] is not None and not alerts_cfg.include_variations:
            continue  # gate 3: variation price is typically the lowest one

        observation = get_latest_observation(conn, item_id)
        selected = select_price(
            observation["total_cents"] if observation else None,
            observation["price_cents"] if observation else None,
        )
        if selected is None:
            continue  # gate 4: no usable price (e.g. an auction with no BIN)
        price_cents, price_is_price_only = selected

        if price_cents > round(alerts_cfg.max_price_usd * 100):
            continue  # gate 5: over the buying ceiling regardless of score

        spec = json.loads(row["spec_json"]) if row["spec_json"] else {}
        result = score_listing(
            conn,
            profile,
            compiled_seeds,
            item_id=item_id,
            bucket_key=row["bucket_key"],
            spec=spec,
            price_cents=price_cents,
            price_is_price_only=price_is_price_only,
            item_web_url=row["item_web_url"],
        )

        if result.ratio_to_p25 > alerts_cfg.trigger.max_ratio_to_p25:
            continue  # gate 6/7: not a good enough deal against the baseline

        prior = last_alert(conn, item_id)
        if prior is not None:
            if now_ts - prior["sent_at"] < alerts_cfg.cooldown_minutes * 60:
                continue  # gate 8a: still in cooldown
            if not _passes_realert_drop(
                price_cents, prior["price_cents"], alerts_cfg.realert_drop_pct
            ):
                continue  # gate 8b: not enough of a further price drop

        survivors.append(result)

    survivors.sort(key=lambda r: r.ratio_to_p25)

    if len(survivors) > alerts_cfg.max_per_cycle:
        logger.info(
            "alert cap reached for profile=%s: %d survivor(s), posting %d, "
            "suppressing %d - trigger.max_ratio_to_p25 may be too loose",
            profile.id,
            len(survivors),
            alerts_cfg.max_per_cycle,
            len(survivors) - alerts_cfg.max_per_cycle,
        )

    return survivors[: alerts_cfg.max_per_cycle]


async def run_alert_cycle(
    conn,
    profile: Profile,
    compiled_seeds: list[CompiledSeedBaseline],
    item_ids: list[str],
    now_ts: int,
) -> None:
    """Evaluate item_ids and, for each survivor, post (or skip posting, in
    dry_run) and write exactly one alerts row - dry_run=1,
    delivery_status='dry_run' when not posting, else whatever send_alert
    returns ('sent' or 'failed').

    A no-op if profile.alerts is None - a profile without an alerts block
    collects and scores but never alerts (AlertsConfig's docstring).

    Callers (engine/collector.py) are responsible for failure isolation:
    this does not catch exceptions itself, so a bug here must not be
    allowed to propagate into a poll/sweep loop uncaught.
    """
    if profile.alerts is None:
        return

    survivors = evaluate(conn, profile, compiled_seeds, item_ids, now_ts)
    if not survivors:
        return

    webhook_url = resolve_webhook_url(profile)
    dry_run = profile.alerts.dry_run

    async with httpx.AsyncClient() as client:
        for result in survivors:
            row = conn.execute(
                "SELECT spec_json FROM listings WHERE item_id = ?", (result.item_id,)
            ).fetchone()
            spec = json.loads(row["spec_json"]) if row and row["spec_json"] else {}

            if dry_run:
                delivery_status = "dry_run"
            else:
                delivery_status = await discord.send_alert(
                    webhook_url, result, spec, profile.alerts, client=client
                )

            record_alert(
                conn,
                result.item_id,
                profile.id,
                now_ts,
                dry_run=dry_run,
                price_cents=result.price_cents,
                price_is_price_only=result.price_is_price_only,
                bucket_key=result.bucket_key,
                baseline_layer=result.baseline_layer,
                baseline_match=result.baseline_match,
                baseline_n=result.baseline_n,
                baseline_p25_cents=result.baseline_p25_cents,
                baseline_p50_cents=result.baseline_p50_cents,
                ratio_to_p25=result.ratio_to_p25,
                sanity_flagged=result.sanity_flagged,
                delivery_status=delivery_status,
            )
