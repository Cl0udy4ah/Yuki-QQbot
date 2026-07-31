"""Destructive, one-way Memory V2 cutover coverage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

_LEGACY_TABLES = {
    "person_memories",
    "group_memories",
    "person_group_memories",
    "person_preferences",
    "memory_jobs",
}
_V2_TABLES = {"memory_facts", "memory_evidence", "memory_jobs"}


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    return Config("alembic.ini")


def _create_legacy_memory_fixture(connection: sqlite3.Connection) -> None:
    for table in _LEGACY_TABLES:
        connection.execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute(f"INSERT INTO {table} (id, value) VALUES (1, 'legacy')")


def test_0020_destroys_only_v1_memory_and_preserves_the_event_ledger(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory-v2-cutover.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0019")
    with sqlite3.connect(path) as connection:
        _create_legacy_memory_fixture(connection)
        connection.execute(
            """
            INSERT INTO people (
                user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at
            ) VALUES ('1001', '张三', 1, 0, '2026-07-31', '2026-07-31')
            """
        )
        connection.execute(
            """
            INSERT INTO groups (
                group_id, name, enabled, require_mention, autonomous_enabled,
                first_seen_at, last_seen_at, updated_at
            ) VALUES (
                '2001', '测试群', 1, 1, 1,
                '2026-07-31', '2026-07-31', '2026-07-31'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memberships (
                user_id, group_id, group_card, first_seen_at, last_seen_at
            ) VALUES ('1001', '2001', '张三同学', '2026-07-31', '2026-07-31')
            """
        )
        connection.execute(
            """
            INSERT INTO chat_events (
                bot_user_id, platform_message_id, scope_type, group_id,
                sender_user_id, direction, content, visual_summary, segments_json,
                origin, occurred_at, observed_at
            ) VALUES (
                '8000', 'event-1', 'group', '2001', '1001', 'inbound',
                '历史聊天保留', '', '[]', 'user_message', '2026-07-31', '2026-07-31'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO person_relationships (
                user_id, affection_score, trust_score, created_at, updated_at
            ) VALUES ('1001', 66, 77, '2026-07-31', '2026-07-31')
            """
        )
        connection.execute(
            """
            INSERT INTO automations (
                creator_user_id, bot_user_id, name, status, timezone,
                schedule_json, script_json, script_hash, required_capabilities_json,
                authority_snapshot_json, created_from_message_id, run_count,
                consecutive_failures, misfire_grace_seconds, created_at, updated_at
            ) VALUES (
                '1001', '8000', '保留任务', 'active', 'Asia/Shanghai',
                '{}', '{}', 'hash', '[]', '{}', 'event-1', 0, 0, 1800,
                '2026-07-31', '2026-07-31'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO plugin_installations (
                plugin_id, name, version, plugin_api, yuki_requires, manifest_hash,
                entrypoint, status, enabled, approved_permissions_json,
                requested_permissions_json, failure_count, discovered_at, updated_at
            ) VALUES (
                'fixture', '保留插件', '1.0.0', '1', '>=2', 'hash',
                'fixture:plugin', 'running', 1, '[]', '[]', 0,
                '2026-07-31', '2026-07-31'
            )
            """
        )
        connection.commit()

    command.upgrade(config, "0020")

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        facts = connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone()
        evidence = connection.execute("SELECT COUNT(*) FROM memory_evidence").fetchone()
        jobs = connection.execute("SELECT COUNT(*) FROM memory_jobs").fetchone()
        identity = connection.execute(
            """
            SELECT p.nickname, g.name, m.group_card
            FROM people AS p
            JOIN memberships AS m ON m.user_id = p.user_id
            JOIN groups AS g ON g.group_id = m.group_id
            """
        ).fetchone()
        event = connection.execute("SELECT content FROM chat_events").fetchone()
        relationship = connection.execute(
            "SELECT affection_score, trust_score FROM person_relationships"
        ).fetchone()
        automation = connection.execute("SELECT name FROM automations").fetchone()
        plugin = connection.execute("SELECT name FROM plugin_installations").fetchone()

    assert revision == ("0020",)
    assert {
        "person_memories",
        "group_memories",
        "person_group_memories",
        "person_preferences",
    }.isdisjoint(tables)
    assert _V2_TABLES <= tables
    assert facts == evidence == jobs == (0,)
    assert identity == ("张三", "测试群", "张三同学")
    assert event == ("历史聊天保留",)
    assert relationship == (66, 77)
    assert automation == ("保留任务",)
    assert plugin == ("保留插件",)

    with pytest.raises(
        RuntimeError,
        match="Memory V2 cutover is irreversible; restore the pre-upgrade database backup\\.",
    ):
        command.downgrade(config, "0019")


def test_fresh_install_creates_empty_memory_v2_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory-v2-fresh.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0020")

    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert revision == ("0020",)
    assert _V2_TABLES <= tables
    assert {
        "uq_memory_facts_active_person_key",
        "uq_memory_facts_active_person_group_key",
        "uq_memory_facts_active_group_key",
    } <= indexes
