"""Yuki single-instance self-memory migration coverage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from tests.unit.test_migration_0021 import _config, _seed_v2


def test_0027_adds_self_visibility_and_preserves_existing_memory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "yuki-self-memory.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0022")
    with sqlite3.connect(path) as connection:
        _seed_v2(connection)
    command.upgrade(config, "0027")

    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0027",)
        assert connection.execute(
            "SELECT visibility_type, visibility_user_id, visibility_group_id "
            "FROM memory_facts WHERE id=1"
        ).fetchone() == (None, None, None)
        connection.execute(
            """
            INSERT INTO memory_facts (
                scope_type, subject_user_id, group_id, visibility_type,
                visibility_user_id, visibility_group_id, kind, memory_key, category,
                content, normalized_content, importance, confidence, source_type,
                authority, status, conflict_state, created_at, updated_at, last_confirmed_at
            ) VALUES (
                'self', NULL, NULL, 'private', '1001', NULL, 'preference',
                'preference:careful', 'self_preference', '偏好认真回答', '偏好认真回答',
                4, 0.8, 'automatic', 'agent_reflection', 'active', 'clear',
                '2026-08-03', '2026-08-03', '2026-08-03'
            )
            """
        )
        self_id = int(connection.execute("SELECT max(id) FROM memory_facts").fetchone()[0])
        connection.execute(
            """
            INSERT INTO memory_evidence (
                fact_id, event_id, source_speaker_user_id, relation, confidence,
                authority, excerpt, created_at
            ) VALUES (?, 1, '1001', 'agent_reflection', 0.8,
                      'agent_reflection', '我准备考研', '2026-08-03')
            """,
            (self_id,),
        )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO memory_facts (
                    scope_type, visibility_type, visibility_user_id, kind, memory_key,
                    category, content, normalized_content, importance, confidence,
                    source_type, authority, status, conflict_state, created_at, updated_at,
                    last_confirmed_at
                ) VALUES (
                    'self', 'private', '1001', 'fact', 'preference:careful',
                    'self_fact', '重复槽位', '重复槽位', 3, 0.7, 'automatic',
                    'agent_reflection', 'active', 'clear', '2026-08-03', '2026-08-03',
                    '2026-08-03'
                )
                """
            )
        connection.commit()

    with pytest.raises(RuntimeError, match="self memories exist"):
        command.downgrade(config, "0026")

    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM memory_facts WHERE scope_type='self'")
        connection.commit()
    command.downgrade(config, "0026")
    with sqlite3.connect(path) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memory_facts)")}
        assert "visibility_type" not in columns
        assert connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (1,)
