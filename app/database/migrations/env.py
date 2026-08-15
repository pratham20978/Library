"""Alembic environment (arch §27).

The URL comes from `Settings`, not from `alembic.ini`, so a migration always
runs against the database the hub itself would open — and no connection string
with a password ends up in a tracked file.

Both drivers the hub supports are async (`postgresql+asyncpg`,
`sqlite+aiosqlite`), so the online path opens an async engine and hands a sync
connection to Alembic through `run_sync`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.config.settings import load_settings
from app.database.models import Base

config = context.config
target_metadata = Base.metadata

_settings = load_settings()
_url = _settings.database_url


def _configure(connection: Connection | None = None, **kwargs: Any) -> None:
    """Shared options for both modes.

    `render_as_batch` matters on SQLite, which cannot ALTER a column in place:
    Alembic instead rebuilds the table. `compare_type` makes autogenerate notice
    a changed column type rather than silently skipping it.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection is not None and connection.dialect.name == "sqlite",
        compare_type=True,
        compare_server_default=True,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Emit SQL without a database — `alembic upgrade head --sql`."""
    _configure(url=_url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Open the configured database and apply the pending revisions."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _url
    engine = async_engine_from_config(section, prefix="sqlalchemy.", poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
