"""Database engine and session lifecycle.

One `Database` per process, created at startup and disposed at shutdown. It
hands out sessions and knows how to bring an empty database up to schema.

Two behaviours worth knowing about:

* **SQLite gets foreign keys turned on.** SQLite disables them per connection by
  default, so `ON DELETE CASCADE` on the tool and version tables would silently
  do nothing and leave orphaned rows behind a removed integration.
* **`create_all` is a development convenience only.** Production schema changes
  go through Alembic (arch §27). `ensure_schema` refuses to create tables under
  a production environment so a migration can never be skipped by accident.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.logging import get_logger
from app.database.models import Base

__all__ = ["Database"]

log = get_logger(__name__)


@event.listens_for(Engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection: Any, _record: Any) -> None:
    """Turn on foreign-key enforcement for SQLite connections.

    Registered against the sync `Engine` because aiosqlite drives one underneath.
    Detection is by module name so PostgreSQL connections are untouched.
    """
    module = type(dbapi_connection).__module__
    if "sqlite" not in module:
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        # WAL lets the API read while a worker writes, instead of blocking.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
    finally:
        cursor.close()


class Database:
    """Owns the async engine and session factory."""

    def __init__(self, url: str, *, echo: bool = False, is_production: bool = False) -> None:
        self._url = url
        self._is_production = is_production
        if url.startswith("sqlite"):
            self._ensure_sqlite_directory(url)
        self._engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            future=True,
            pool_pre_ping=True,
            # SQLite's async driver serialises anyway; a pool size would be noise.
            **({} if url.startswith("sqlite") else {"pool_size": 10, "max_overflow": 20}),
        )
        self._sessions: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False, class_=AsyncSession
        )

    @staticmethod
    def _ensure_sqlite_directory(url: str) -> None:
        """Create the parent directory so a first run does not fail on a missing path."""
        _, _, path_part = url.partition(":///")
        if not path_part or path_part == ":memory:":
            return
        Path(path_part).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)

    @property
    def engine(self) -> AsyncEngine:
        """The underlying engine, for Alembic and diagnostics."""
        return self._engine

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        """Factory used by components that manage their own transactions."""
        return self._sessions

    @property
    def url(self) -> str:
        """The configured URL. May contain a password — redact before logging."""
        return self._url

    @property
    def dialect(self) -> str:
        """`sqlite` or `postgresql`, for dialect-dependent behaviour."""
        return self._engine.dialect.name

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """A session that commits on success and rolls back on failure."""
        async with self._sessions() as session:
            try:
                yield session
                await session.commit()
            except BaseException:
                await session.rollback()
                raise

    async def ensure_schema(self) -> None:
        """Create tables if they do not exist.

        Raises:
            RuntimeError: Called in production, where Alembic owns the schema.
        """
        if self._is_production:
            raise RuntimeError(
                "Refusing to create tables automatically in production. "
                "Run `make migrate` (alembic upgrade head) instead — arch §27."
            )
        async with self._engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        log.info("database.schema_ready", dialect=self.dialect)

    async def ping(self) -> bool:
        """Whether the database answers. Used by `/ready` and `mcp-hub doctor`."""
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            return True
        except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
            log.warning("database.ping_failed", error=str(exc))
            return False

    async def dispose(self) -> None:
        """Close every pooled connection."""
        await self._engine.dispose()
        log.info("database.disposed")
