"""Profile config schema - the shape of `profiles/*.yaml`.

This models only what V0.3 needs (identity fields + `search`), which is why
extra keys are ignored rather than rejected: a real profile file like
profiles/thinkpad-t14.yaml already has reject/extract/derive/tiers/scoring/
seed_baselines/alerts sections that later milestones (V0.4+) will model
here. Rejecting them now would mean today's client can't load a real
profile at all.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict


class PollConfig(BaseModel):
    interval_minutes: int = 5
    sweep_interval_minutes: int = 60
    sort: str | None = None


class SearchConfig(BaseModel):
    queries: list[str]
    # Best-effort only - Browse's negative-term handling in `q` is
    # inconsistent, so this is not authoritative filtering. See
    # profiles/thinkpad-t14.yaml's comment on this field.
    query_exclude: list[str] = []
    category_ids: list[str] = []
    # eBay's own filter grammar, e.g. {"price": [80, 1200], "conditionIds":
    # [2000, 3000]} - kept as a loose dict rather than a typed model because
    # it's passed straight through to build_filter_string().
    filters: dict[str, Any] = {}
    poll: PollConfig = PollConfig()


class Profile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str
    name: str
    enabled: bool = True
    provider: str = "ebay"
    schema_version: int = 1
    search: SearchConfig
