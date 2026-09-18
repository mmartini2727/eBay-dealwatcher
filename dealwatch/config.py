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
    # Pacific. reserve_calls is headroom kept back for manual use after the
    # collector has spent its share - NOT for the MCP server (V1.0,
    # design.md §15's D3): that server makes zero eBay calls, has no eBay
    # credentials, and never touches DailyBudget at all. The reserve exists
    # purely for a human running an ad-hoc script against the same budget
    # counter.
    daily_call_limit: int = 5000
    daily_reserve_calls: int = 250

    log_level: str = "INFO"

    # Which profiles/*.yaml the collector runs. Not a schema change -
    # Profile itself is unmodified; this just points at a file for it.
    profile_path: str = "profiles/thinkpad-t14.yaml"

    # V1.0 (design.md §15 D8): comma-separated Host headers the MCP
    # server's transport-security layer accepts, e.g.
    # "192.168.99.204:8088,127.0.0.1:*,localhost:*". The real LAN value
    # comes from compose.yaml's dealwatch-mcp service env, not .env - it
    # isn't a secret. Defaults cover local/dev use only; a LAN deployment
    # must set the real host:port or every real request gets 421.
    mcp_allowed_hosts: str = "127.0.0.1:*,localhost:*"

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()