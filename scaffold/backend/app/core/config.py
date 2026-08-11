"""Environment-based configuration. No secrets in code, no literals in modules."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Core ---
    app_name: str = "job-orchestrator"
    env: str = Field(default="local", description="local | staging | production")
    log_level: str = "INFO"
    # App Platform injects PORT; default matches docker-compose.
    port: int = 8000

    # --- Database ---
    database_url: str = "postgresql://postgres:postgres@db:5432/jobs"
    db_pool_min: int = 2
    db_pool_max: int = 10

    # --- Worker / orchestration ---
    worker_enabled: bool = True
    worker_concurrency: int = 4
    worker_batch_size: int = 5
    worker_poll_interval_s: float = 1.0
    lease_seconds: int = 30
    heartbeat_interval_s: float = 10.0
    reaper_interval_s: float = 15.0
    max_attempts: int = 4
    backoff_base_s: float = 2.0
    backoff_max_s: float = 60.0

    # --- Simulated downstream dependency (stands in for a real external API) ---
    dependency_failure_rate: float = Field(default=0.3, ge=0.0, le=1.0)
    dependency_latency_ms: int = 300

    # --- Read model ---
    matview_refresh_interval_s: float = 10.0

    @property
    def asyncpg_dsn(self) -> str:
        """asyncpg does not accept libpq's ``sslmode`` in every form.

        DigitalOcean Managed Postgres hands you a ``postgresql://...?sslmode=require`` URI,
        so normalise it here rather than at every call site.
        """
        parts = urlsplit(self.database_url)
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "sslmode"]
        scheme = "postgresql" if parts.scheme in ("postgres", "postgresql") else parts.scheme
        return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), ""))

    @property
    def require_ssl(self) -> bool:
        return "sslmode=require" in self.database_url or "sslmode=verify" in self.database_url


settings = Settings()
