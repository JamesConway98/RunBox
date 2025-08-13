from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://runbox:runbox@localhost:5432/runbox"
    redis_url: str = "redis://localhost:6379/0"

    # Pool sizing. The control plane is IO-bound and does almost no CPU work, so
    # this is about how many concurrent SSE streams we are willing to hold open
    # rather than about throughput.
    db_pool_min: int = 2
    db_pool_max: int = 20

    # Guardrails on what a caller may ask for. The runner enforces its own
    # ceiling too; this one exists so a bad request is rejected at the edge
    # rather than after a container has been created.
    max_timeout_s: int = 600
    max_tokens_ceiling: int = 200_000
    max_task_chars: int = 20_000

    # SSE. A heartbeat keeps intermediaries from reaping an idle connection —
    # a run that spends 40s inside one tool call looks dead without it.
    sse_heartbeat_s: float = 15.0
    sse_replay_page: int = 500

    demo_tenant_slug: str = "demo"
    demo_rate_limit_per_hour: int = 5

    cors_origins: str = "http://localhost:3000"
    log_level: str = "info"

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()
