"""Memory V2 lexical-index migration and lifecycle coverage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    return Config("alembic.ini")


def _seed_v2(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO people (user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at)
        VALUES ('1001', '张三', 1, 0, '2026-08-01', '2026-08-01')
        """
    )
    connection.execute(
        """
        INSERT INTO chat_events (
            bot_user_id, platform_message_id, scope_type, private_peer_user_id,
            sender_user_id, direction, content, visual_summary, segments_json,
            origin, occurred_at, observed_at
        ) VALUES (
            '8000', 'event-1', 'private', '1001', '1001', 'inbound',
            '我准备考研', '', '[]', 'user_message', '2026-08-01', '2026-08-01'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO memory_facts (
            id, scope_type, subject_user_id, kind, memory_key, category,
            content, normalized_content, importance, confidence, source_type,
            status, created_at, updated_at
        ) VALUES (
            1, 'person', '1001', 'fact', 'education:plan', 'education',
            '准备考研', '准备考研', 4, 0.9, 'automatic', 'active',
            '2026-08-01', '2026-08-01'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO memory_evidence (
            fact_id, event_id, source_speaker_user_id, relation, excerpt, created_at
        ) VALUES (1, 1, '1001', 'self_statement', '我准备考研', '2026-08-01')
        """
    )
    connection.commit()


def _match(connection: sqlite3.Connection, query: str) -> tuple[int, ...]:
    return tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT rowid FROM memory_facts_fts WHERE memory_facts_fts MATCH ? ORDER BY rowid",
            (f'"{query}"',),
        )
    )


def test_0021_backfills_preserves_and_tracks_fact_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory-fts.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0020")
    with sqlite3.connect(path) as connection:
        _seed_v2(connection)

    command.upgrade(config, "0021")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0021",)
        assert connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM memory_evidence").fetchone() == (1,)
        assert _match(connection, "准备考研") == (1,)
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE 'memory_facts_fts_%'"
            )
        }
        assert triggers == {
            "memory_facts_fts_ai",
            "memory_facts_fts_ad",
            "memory_facts_fts_au",
        }

        connection.execute(
            "UPDATE memory_facts SET content = '决定工作', "
            "normalized_content = '决定工作' WHERE id = 1"
        )
        assert _match(connection, "准备考研") == ()
        assert _match(connection, "决定工作") == (1,)

        connection.execute(
            """
            INSERT INTO memory_facts (
                id, scope_type, subject_user_id, kind, memory_key, category,
                content, normalized_content, importance, confidence, source_type,
                status, created_at, updated_at
            ) VALUES (
                2, 'person', '1001', 'fact', 'city', 'profile', '住在杭州',
                '住在杭州', 3, 1.0, 'explicit', 'active', '2026-08-01', '2026-08-01'
            )
            """
        )
        assert _match(connection, "住在杭州") == (2,)
        connection.execute("UPDATE memory_facts SET status = 'invalidated' WHERE id = 2")
        active = connection.execute(
            """
            SELECT mf.id FROM memory_facts_fts
            JOIN memory_facts AS mf ON mf.id = memory_facts_fts.rowid
            WHERE memory_facts_fts MATCH '"住在杭州"' AND mf.status = 'active'
            """
        ).fetchall()
        assert active == []
        connection.execute("DELETE FROM memory_facts WHERE id = 2")
        assert _match(connection, "住在杭州") == ()

        connection.execute("INSERT INTO memory_facts_fts(memory_facts_fts) VALUES ('rebuild')")
        connection.execute("INSERT INTO memory_facts_fts(memory_facts_fts) VALUES ('rebuild')")
        assert connection.execute("SELECT COUNT(*) FROM memory_facts_fts_docsize").fetchone() == (
            1,
        )
        connection.commit()

    command.downgrade(config, "0020")
    with sqlite3.connect(path) as connection:
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "memory_facts_fts" not in names
        assert not any(name.startswith("memory_facts_fts_") for name in names)
        assert connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM memory_evidence").fetchone() == (1,)
