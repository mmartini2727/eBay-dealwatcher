from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment variables / .env."""

    ebay_client_id: str | None = None
    ebay_client_secret: str | None = None
    ebay_marketplace_id: str = "EBAY_US"
    ebay_location_country: str = "US"
    ebay_location_zip: str | None = None

    # SQLite file. The budget table lives here from V0.3; listings/baselines/
    # alerts tables land at V0.5.
    db_path: str = "data/dealwatch.db"

    # Browse API budget (design.md §7): 5,000/day, app-level, resets midnight
    # Pacific. reserve_calls is headroom kept back for manual/MCP queries
    # after the collector has spent its share.
    daily_call_limit: int = 5000
    daily_reserve_calls: int = 250

    log_level: str = "INFO"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()