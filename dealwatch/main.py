import asyncio
import logging
from contextlib import asynccontextmanager
from functools import lru_cache

from fastapi import Depends, FastAPI, Request

from dealwatch.config import get_settings
from dealwatch.engine.collector import Collector, CollectorStats, load_profile
from dealwatch.providers.ratelimit import DailyBudget


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
    collector: Collector | None = None

    # Without credentials, TokenManager fails on its first real mint
    # attempt - starting the loops anyway would just spam that failure
    # every cycle. Skip cleanly instead, e.g. for a checkout with no .env.
    if live_settings.ebay_client_id and live_settings.ebay_client_secret:
        profile = load_profile(live_settings.profile_path)
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
