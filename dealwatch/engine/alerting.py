"""Alert evaluation and delivery (V0.9, design.md's dated entry): decides
which active listings deserve an alert this cycle, and drives delivery +
persistence for the survivors.

evaluate() is the decision logic and is pure aside from reading conn - no
network, no writes. run_alert_cycle() is the driver: it calls evaluate(),
then for each survivor, attempts every configured notifier (V0.9a - see
resolve_notifier_credentials and _NOTIFIER_MODULES below) and writes one
`alerts` row per notifier, dry-run or not (storage/sqlite.py's last_alert()
docstring explains why dry-run rows are written at all).

No buyability/suppression rules here (soldered RAM, minimum specs) - V0.9a
scope note (design.md §5.7): labeling only, no suppression. No scores
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
from dealwatch.notify import discord, pushover
from dealwatch.storage.sqlite import get_latest_observation, last_alert, record_alert

logger = logging.getLogger(__name__)

# Fixed env var names for Pushover, unlike Discord's `webhook_env` - a
# profile can point at any environment variable name it likes for its
# webhook, but Pushover's credential pair is a per-deployment secret, not
# a per-profile one worth naming in YAML (nothing else about Pushover is
# profile-specific).
PUSHOVER_APP_TOKEN_ENV = "PUSHOVER_APP_TOKEN"
PUSHOVER_USER_KEY_ENV = "PUSHOVER_USER_KEY"

# name -> module, not name -> bound function. Tests monkeypatch
# dealwatch.notify.discord.send_alert directly (module-attribute patch, no
# pluggable-transport abstraction - CLAUDE.md); looking up `.send_alert` on
# the module object at call time, rather than capturing the function
# reference once at import time, is what makes that patch visible here.
# This is dispatch, not a notifier abstraction: two concrete modules behind
# a dict, nothing more (V0.9a out-of-scope note).
_NOTIFIER_MODULES = {"discord": discord, "pushover": pushover}

# Sentinel written to alerts.notifier when alerts.notifiers is empty -
# see run_alert_cycle's docstring for why a row is written at all in that
# case.
_NO_NOTIFIER = "none"

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


def resolve_notifier_credentials(profile: Profile) -> dict[str, object]:
    """{} if profile.alerts is None or alerts.notifiers is empty - both
    mean "no notifier credentials to check." Otherwise, one entry per name
    in alerts.notifiers: "discord" -> the webhook URL (via
    resolve_webhook_url, unchanged), "pushover" -> an (app_token, user_key)
    tuple read from the fixed PUSHOVER_APP_TOKEN_ENV/PUSHOVER_USER_KEY_ENV
    names above. schema.py's `Literal["discord", "pushover"]` type on
    `notifiers` already rejects any other value at profile-load time, so
    every name reaching this loop is one of exactly these two - no `else`
    branch needed.

    Any missing/empty credential is a ProfileCompileError, the same
    "startup error, not a silent no-op discovered at 2am" family as
    resolve_webhook_url and every other compile_profile check. Called once
    at startup (Collector.__init__) AND once per run_alert_cycle call, same
    reasoning as resolve_webhook_url's own docstring: re-reading a handful
    of already-set environment variables is a dict lookup, not a cost worth
    caching.
    """
    if profile.alerts is None:
        return {}

    credentials: dict[str, object] = {}
    for name in profile.alerts.notifiers:
        if name == "discord":
            credentials["discord"] = resolve_webhook_url(profile)
        elif name == "pushover":
            app_token = os.environ.get(PUSHOVER_APP_TOKEN_ENV)
            user_key = os.environ.get(PUSHOVER_USER_KEY_ENV)
            if not app_token or not user_key:
                raise ProfileCompileError(
                    "alerts.notifiers includes 'pushover', but "
                    f"{PUSHOVER_APP_TOKEN_ENV} and {PUSHOVER_USER_KEY_ENV} "
                    "must both be set and non-empty in the environment"
                )
            credentials["pushover"] = (app_token, user_key)
    return credentials


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

        bucket_key = row["bucket_key"]
        if alerts_cfg.require_complete_bucket and (
            bucket_key is None or "?" in bucket_key
        ):
            continue  # gate 4: too incomplete to build a baseline from - see
            # engine/baselines.py's identical no-question-mark filter. Placed
            # before scoring, not after: there is no reason to resolve a
            # baseline and compute a ratio for a listing that can never
            # alert regardless of the result.

        observation = get_latest_observation(conn, item_id)
        selected = select_price(
            observation["total_cents"] if observation else None,
            observation["price_cents"] if observation else None,
        )
        if selected is None:
            continue  # gate 5: no usable price (e.g. an auction with no BIN)
        price_cents, price_is_price_only = selected

        if price_cents > round(alerts_cfg.max_price_usd * 100):
            continue  # gate 6: over the buying ceiling regardless of score

        spec = json.loads(row["spec_json"]) if row["spec_json"] else {}
        result = score_listing(
            conn,
            profile,
            compiled_seeds,
            item_id=item_id,
            bucket_key=bucket_key,
            spec=spec,
            price_cents=price_cents,
            price_is_price_only=price_is_price_only,
            item_web_url=row["item_web_url"],
        )

        if result.ratio_to_p25 > alerts_cfg.trigger.max_ratio_to_p25:
            continue  # gate 7: not a good enough deal against the baseline

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
    """Evaluate item_ids and, for each survivor, attempt every notifier in
    alerts.notifiers and write one alerts row per notifier - dry_run=1,
    delivery_status='dry_run' for each when not actually posting, else
    whatever that notifier's send_alert returns ('sent' or 'failed').

    Each notifier is attempted independently: one raising must not prevent
    the others from being tried (V0.9a) - a Pushover outage must not cost
    the user the Discord alert they'd otherwise have gotten, and vice
    versa. All of a survivor's rows are written in a single transaction,
    after every notifier for that survivor has been attempted - a crash
    mid-send must not leave the item with rows for some notifiers and not
    others, since last_alert() (storage/sqlite.py) reads across notifiers
    and a partial write would leave dedup state inconsistent with what was
    actually sent.

    alerts.notifiers=[] is valid and sends nothing, but still writes one
    row per survivor with notifier='none', delivery_status='skipped' -
    same reasoning as the dry-run rows (storage/sqlite.py's last_alert()
    docstring): without a row, evaluate()'s cooldown/re-alert gate has
    nothing to dedup against, and every cycle would re-evaluate this
    survivor as if it had never been seen.

    A no-op if profile.alerts is None - a profile without an alerts block
    collects and scores but never alerts (AlertsConfig's docstring).

    Callers (engine/collector.py) are responsible for failure isolation at
    the cycle level: this does not catch every exception itself (a bug in
    evaluate() or in the transaction below still propagates), so a bug here
    must not be allowed to propagate into a poll/sweep loop uncaught.
    """
    if profile.alerts is None:
        return

    survivors = evaluate(conn, profile, compiled_seeds, item_ids, now_ts)
    if not survivors:
        return

    alerts_cfg = profile.alerts
    dry_run = alerts_cfg.dry_run
    credentials = resolve_notifier_credentials(profile)

    async with httpx.AsyncClient() as client:
        for result in survivors:
            row = conn.execute(
                "SELECT spec_json FROM listings WHERE item_id = ?", (result.item_id,)
            ).fetchone()
            spec = json.loads(row["spec_json"]) if row and row["spec_json"] else {}

            delivery_statuses: dict[str, str] = {}
            if not alerts_cfg.notifiers:
                delivery_statuses[_NO_NOTIFIER] = "skipped"
            else:
                for name in alerts_cfg.notifiers:
                    if dry_run:
                        delivery_statuses[name] = "dry_run"
                        continue
                    try:
                        send_alert = _NOTIFIER_MODULES[name].send_alert
                        delivery_statuses[name] = await send_alert(
                            credentials[name], result, spec, alerts_cfg, client=client
                        )
                    except Exception:
                        logger.exception(
                            "notifier=%s raised for item_id=%s",
                            name,
                            result.item_id,
                        )
                        delivery_statuses[name] = "failed"

            conn.execute("BEGIN IMMEDIATE")
            try:
                for name, delivery_status in delivery_statuses.items():
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
                        notifier=name,
                    )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
