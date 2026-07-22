"""Database engine lifecycle and health checks."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from qq_ai_bot.persistence.models import Base


class Database:
    """Own the async SQLAlchemy engine and explicit session factory."""

    def __init__(self, url: str) -> None:
        self.url = url
        self._ensure_sqlite_parent(url)
        self.engine: AsyncEngine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @staticmethod
    def _ensure_sqlite_parent(url: str) -> None:
        prefix = "sqlite+aiosqlite:///"
        if not url.startswith(prefix):
            return
        path = Path(url.removeprefix(prefix))
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)

    async def create_schema(self) -> None:
        """Create all tables for tests; deployments use Alembic migrations."""

        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async def ping(self) -> bool:
        """Check database connectivity without exposing its path."""

        try:
            async with self.sessions() as session:
                await session.execute(text("SELECT 1"))
            return True
        except (OSError, RuntimeError, SQLAlchemyError):
            return False

    async def close(self) -> None:
        """Dispose pooled database connections."""

        await self.engine.dispose()
