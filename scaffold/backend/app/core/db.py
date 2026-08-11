"""Connection pool + migration runner.

asyncpg is used directly rather than through an ORM: the claim query is the heart of
this service and it should be readable as SQL, not assembled by a query builder.
"""

from __future__ import annotations

import pathlib
import ssl as ssl_module

import asyncpg

from app.core.config import settings
from app.core.logging import get_logger

log = get_logger(__name__)

MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parents[2] / "migrations"

_pool: asyncpg.Pool | None = None


def _ssl_context():
    if not settings.require_ssl:
        return None
    # DO Managed Postgres presents a CA the container may not carry; the connection is
    # still encrypted. For production, ship the DO CA bundle and verify properly.
    ctx = ssl_module.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl_module.CERT_NONE
    return ctx


async def connect() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(
            dsn=settings.asyncpg_dsn,
            min_size=settings.db_pool_min,
            max_size=settings.db_pool_max,
            ssl=_ssl_context(),
            command_timeout=30,
        )
        log.info("db.pool.created", min=settings.db_pool_min, max=settings.db_pool_max)
    return _pool


async def disconnect() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("database pool not initialised")
    return _pool


async def raw_connection() -> asyncpg.Connection:
    """A dedicated connection outside the pool — required for LISTEN."""
    return await asyncpg.connect(dsn=settings.asyncpg_dsn, ssl=_ssl_context())


async def run_migrations() -> None:
    """Apply .sql files in order, recording each in schema_migrations.

    Plain SQL files rather than Alembic: fewer moving parts, and it runs identically
    as an App Platform PRE_DEPLOY job. Alembic is the right call once the schema
    starts needing real down-migrations.
    """
    p = await connect()
    async with p.acquire() as conn:
        # The advisory lock is taken FIRST, before any DDL, and held for the whole
        # process. N replicas boot simultaneously and all call this; the lock makes
        # them serialise so exactly one migrates and the rest no-op.
        #
        # This must wrap the bookkeeping table too: CREATE TABLE IF NOT EXISTS is
        # *not* atomic in Postgres — two concurrent calls race on an internal catalogue
        # index and one loses with a duplicate-key error. (Observed, not theorised.)
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(918_273_645)")
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            applied = {
                r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")
            }
            for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if path.name in applied:
                    continue
                log.info("db.migration.applying", version=path.name)
                await conn.execute(path.read_text())
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1) "
                    "ON CONFLICT DO NOTHING",
                    path.name,
                )
                log.info("db.migration.applied", version=path.name)
