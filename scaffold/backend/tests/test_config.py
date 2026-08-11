"""Config tests — specifically the DigitalOcean DSN shape, which is a real footgun.

DO Managed Postgres hands you `postgresql://...?sslmode=require`. Getting this wrong
fails at deploy time, not locally, which is the worst time to discover it.
"""

from __future__ import annotations

from app.core.config import Settings


def test_local_dsn_passes_through():
    s = Settings(database_url="postgresql://u:p@db:5432/jobs")
    assert s.asyncpg_dsn == "postgresql://u:p@db:5432/jobs"
    assert s.require_ssl is False


def test_do_managed_dsn_strips_sslmode_but_keeps_ssl_on():
    s = Settings(
        database_url="postgresql://doadmin:pw@db-x.b.db.ondigitalocean.com:25060/defaultdb?sslmode=require"
    )
    assert "sslmode" not in s.asyncpg_dsn
    assert s.asyncpg_dsn.endswith("/defaultdb")
    assert s.require_ssl is True


def test_postgres_scheme_is_normalised():
    s = Settings(database_url="postgres://u:p@h:5432/d")
    assert s.asyncpg_dsn.startswith("postgresql://")


def test_other_query_params_survive():
    s = Settings(database_url="postgresql://u:p@h:5432/d?sslmode=require&application_name=api")
    assert "application_name=api" in s.asyncpg_dsn
    assert "sslmode" not in s.asyncpg_dsn


def test_failure_rate_is_bounded():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(dependency_failure_rate=1.5)
