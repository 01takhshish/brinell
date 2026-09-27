"""Runtime configuration. Every value is validated at import time so a misconfigured
process fails at boot rather than on the first request."""

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: Committed to this repository, and therefore public. Refused when env=prod.
DEV_JWT_SECRET = "dev-secret-change-me-please-32-chars-min"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    env: Literal["dev", "test", "prod"] = "dev"
    log_level: str = "INFO"
    log_json: bool = False

    api_host: str = "0.0.0.0"
    api_port: int = 8000
    app_url: str = "http://localhost:3200"
    api_public_url: str = "http://localhost:8000"

    # Postgres. The runner uses the same database as the API: the work queue lives in Postgres
    # (see docs/adr/0001) so claiming a case and writing its result share one transaction.
    database_url: str = "postgresql+asyncpg://crucible:crucible@localhost:5434/crucible"
    db_pool_size: int = 10
    db_max_overflow: int = 5
    db_statement_timeout_ms: int = 30_000

    redis_url: str = "redis://localhost:6380/0"

    # The default is deliberately a sentence that reads as a mistake in a log line. It is refused
    # outright when env=prod — see `_production_requires_real_secrets`.
    jwt_secret: str = Field(default=DEV_JWT_SECRET, min_length=32)
    access_token_ttl_seconds: int = 3600
    cookie_secure: bool = False

    # Provider credentials. Absent keys simply disable that provider; the mock provider always works.
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    # Global safety rails for eval runs.
    max_run_concurrency: int = 32
    default_run_concurrency: int = 8
    case_lease_seconds: int = 120
    max_case_attempts: int = 3
    worker_poll_interval_ms: int = 500
    worker_batch_size: int = 4

    # Deterministic response cache (Redis). Identical (provider, model, params, messages) -> reuse.
    response_cache_enabled: bool = True
    response_cache_ttl_seconds: int = 14 * 24 * 3600

    judge_model: str = "claude-haiku-4-5"
    embedding_dim: int = 256

    @field_validator("database_url")
    @classmethod
    def _require_async_driver(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("database_url must use the postgresql+asyncpg driver")
        return v

    @model_validator(mode="after")
    def _production_requires_real_secrets(self) -> "Settings":
        """Refuse to boot a production process that is still holding development defaults.

        `min_length=32` on the secret is satisfied by the default, which is the problem: the check
        that exists passes, and the one that matters — *is this value public* — was never made. The
        default string is committed to this repository, so a deployment that never set
        `JWT_SECRET` signs its sessions with a key any reader of the source can reproduce,
        and every session on that instance is forgeable. Nothing about that failure is visible from
        the outside: the service works perfectly.

        Cookies are the same shape of problem one layer down. A session cookie sent without
        `Secure` over an `https://` origin is one downgraded request away from being readable, and
        the default is `False` because local development has no TLS.

        Both are refusals rather than warnings. A warning at boot scrolls past in a deploy log and
        is never seen again; the whole point of validating configuration at import time is that a
        misconfigured process does not start.
        """
        if self.env != "prod":
            return self

        problems: list[str] = []
        if self.jwt_secret == DEV_JWT_SECRET:
            problems.append("jwt_secret is still the development default — set JWT_SECRET")
        if self.app_url.startswith("https://") and not self.cookie_secure:
            problems.append("cookie_secure is False but app_url is https — set COOKIE_SECURE=true")

        if problems:
            raise ValueError("refusing to start with env=prod: " + "; ".join(problems))
        return self

    @property
    def sync_database_url(self) -> str:
        """Alembic runs synchronously, so it needs a sync driver — psycopg3 rather than asyncpg.

        Deriving it from the async URL rather than configuring it separately means migrations can
        never be applied to a different database than the one the app talks to.
        """
        return self.database_url.replace("postgresql+asyncpg://", "postgresql+psycopg://")

    @property
    def is_test(self) -> bool:
        return self.env == "test"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
