import logging

from fastapi import FastAPI

from dealwatch.config import get_settings


settings = get_settings()

logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO)
)

app = FastAPI(
    title="DealWatch",
    version="0.1.0",
    description="Generic deal-monitoring engine and MCP service.",
)


@app.get("/health", tags=["system"])
async def health() -> dict[str, str]:
    return {"status": "ok"}
