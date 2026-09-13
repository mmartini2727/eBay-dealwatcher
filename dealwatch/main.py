import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from dealwatch.config import get_settings
from dealwatch.engine.collector import Collector, CollectorStats, load_profile
from dealwatch.providers.ratelimit import DailyBudget
from dealwatch.reporting.dashboard_data import get_payload


settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO)
)

logger = logging.getLogger(__name__)


# lru_cache mirrors get_settings(): one DailyBudget (and its one SQLite
# connection) shared across requests, not re-opened on every /health hit.
# Pydantic Settings objects aren't hashable, so this can't take settings as
# a Depends() parameter the way handlers normally would - it calls
# get_settings() directly instead, same as the module-level `settings`
# above. Tests override behavior via app.dependency_overrides[get_budget].
@lru_cache
def get_budget() -> DailyBudget:
    return DailyBudget(get_settings())


@asynccontextmanager
async def lifespan(app: FastAPI):
    live_settings = get_settings()

    # V0.11 B1: loaded unconditionally, before the credentials check below
    # - parsing a profile YAML needs no eBay credentials, and the
    # dashboard must render whether or not the collector actually started.
    # A missing-credentials container is exactly the moment you most want
    # to look at this page, not a moment it should 500.
    profile = load_profile(live_settings.profile_path)
    app.state.profile = profile

    collector: Collector | None = None

    # Without credentials, TokenManager fails on its first real mint
    # attempt - starting the loops anyway would just spam that failure
    # every cycle. Skip cleanly instead, e.g. for a checkout with no .env.
    if live_settings.ebay_client_id and live_settings.ebay_client_secret:
        collector = Collector(live_settings, profile)
        collector.start()
    else:
        logger.warning(
            "EBAY_CLIENT_ID/EBAY_CLIENT_SECRET not configured; collector not started"
        )

    app.state.collector = collector
    try:
        yield
    finally:
        if collector is not None:
            await collector.stop()


app = FastAPI(
    title="DealWatch",
    version="0.1.0",
    description="Generic deal-monitoring engine and MCP service.",
    lifespan=lifespan,
)

# V0.11 B3: resolved from __file__, never a relative "templates" - that
# works from the repo root on the Mac and breaks under any other working
# directory (a Docker CMD, a systemd unit, pytest run from elsewhere).
templates = Jinja2Templates(directory=Path(__file__).parent / "dashboard" / "templates")


@app.get("/health", tags=["system"])
async def health(request: Request, budget: DailyBudget = Depends(get_budget)) -> dict:
    # DailyBudget.status() does blocking SQLite I/O; to_thread keeps it off
    # the event loop.
    budget_status = await asyncio.to_thread(budget.status)

    collector: Collector | None = request.app.state.collector
    collector_status = (
        collector.stats.to_dict() if collector is not None else CollectorStats().to_dict()
    )

    return {"status": "ok", "budget": budget_status, "collector": collector_status}


@app.get("/", response_class=HTMLResponse, tags=["dashboard"])
def dashboard(request: Request) -> HTMLResponse:
    """V0.11 (design.md §13). LAN-only, unauthenticated, read-only - same
    posture as the rest of DealWatch (CLAUDE.md locked decision #2:
    nothing here is internet-exposed).

    Deliberately `def`, not `async def` (B2's decision, load-bearing, not
    a style choice): the collector runs in-process on this same event
    loop, and get_payload() does blocking SQLite I/O underneath. FastAPI
    runs a sync `def` route in Starlette's anyio threadpool, which gives
    this request its own worker thread and therefore its own SQLite
    connection - sqlite3 connections are check_same_thread=True, so this
    request cannot reuse the collector's connection even if it wanted to.
    An `async def` version of this same body would run ON the event loop
    and block every in-flight poll/sweep for as long as this render takes.
    get_payload() opens its own connection via connect_readonly() - this
    handler never touches the collector's connection at all.
    """
    live_settings = get_settings()
    profile = request.app.state.profile

    # profile.alerts is Optional (a profile with no `alerts:` block collects
    # and scores but never alerts - normalize/schema.py's own docstring).
    # Treated the same as an explicit dry_run=True/no notifiers here: there
    # is nothing live to report on, so the dashboard should say so rather
    # than crash on a None.
    alerts_config = profile.alerts
    dry_run = alerts_config.dry_run if alerts_config is not None else True
    notifiers = alerts_config.notifiers if alerts_config is not None else []

    # search.poll.sweep_interval_minutes, not a top-level Profile field -
    # Profile is extra="ignore" (normalize/schema.py), so a typo'd path
    # here would fail silently rather than at startup. Read through the
    # model, never hardcoded.
    #
    # profile.scoring is a loose dict (normalize/schema.py) - min_samples
    # and fast_lifespan_hours fall back to the exact same defaults (12,
    # 24) scripts/recompute_baselines.py and scripts/baseline_report.py
    # already use, so the dashboard's baseline queue can't silently
    # disagree with either script about what "qualifies" or "fast" means.
    payload = get_payload(
        live_settings.db_path,
        profile_id=profile.id,
        sweep_interval_minutes=profile.search.poll.sweep_interval_minutes,
        dry_run=dry_run,
        notifiers=notifiers,
        ceiling=live_settings.daily_call_limit - live_settings.daily_reserve_calls,
        daily_call_limit=live_settings.daily_call_limit,
        daily_reserve_calls=live_settings.daily_reserve_calls,
        min_samples=profile.scoring.get("min_samples", 12),
        fast_lifespan_hours=profile.scoring.get("fast_lifespan_hours", 24),
    )

    return templates.TemplateResponse(request, "dashboard.html", {"payload": payload})
