import asyncio
import logging
from functools import lru_cache

from fastapi import Depends, FastAPI

from dealwatch.config import get_settings
from dealwatch.providers.ratelimit import DailyBudget


settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO)
)

app = FastAPI(
    title="DealWatch",
    version="0.1.0",
    description="Generic deal-monitoring engine and MCP service.",
)


# lru_cache mirrors get_settings(): one DailyBudget (and its one SQLite
# connection) shared across requests, not re-opened on every /health hit.
# Pydantic Settings objects aren't hashable, so this can't take settings as
# a Depends() parameter the way handlers normally would - it calls
# get_settings() directly instead, same as the module-level `settings`
# above. Tests override behavior via app.dependency_overrides[get_budget].
@lru_cache
def get_budget() -> DailyBudget:
    return DailyBudget(get_settings())


@app.get("/health", tags=["system"])
async def health(budget: DailyBudget = Depends(get_budget)) -> dict:
    # DailyBudget.status() does blocking SQLite I/O; to_thread keeps it off
    # the event loop.
    budget_status = await asyncio.to_thread(budget.status)
    return {"status": "ok", "budget": budget_status}
