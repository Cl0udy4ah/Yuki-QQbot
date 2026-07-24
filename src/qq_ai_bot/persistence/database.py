"""Database engine lifecycle and health checks."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event, text
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
        if url.startswith("sqlite+aiosqlite:///"):
            event.listen(self.engine.sync_engine, "connect", self._enable_sqlite_foreign_keys)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False, class_=AsyncSession)

    @staticmethod
    def _enable_sqlite_foreign_keys(
        dbapi_connection: Any,
        _connection_record: Any,
    ) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

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
            await self._create_fts_schema(connection)

    @staticmethod
    async def _create_fts_schema(connection: Any) -> None:
        """Create the external-content FTS index used by the event ledger."""

        statements = (
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS chat_events_fts USING fts5(
                content,
                content='chat_events',
                content_rowid='id',
                tokenize='trigram'
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS chat_events_fts_ai
            AFTER INSERT ON chat_events BEGIN
                INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS chat_events_fts_ad
            AFTER DELETE ON chat_events BEGIN
                INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS chat_events_fts_au
            AFTER UPDATE OF content ON chat_events BEGIN
                INSERT INTO chat_events_fts(chat_events_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
                INSERT INTO chat_events_fts(rowid, content) VALUES (new.id, new.content);
            END
            """,
        )
        for statement in statements:
            await connection.execute(text(statement))

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
