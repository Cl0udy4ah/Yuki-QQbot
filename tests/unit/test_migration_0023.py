"""Memory V2 conflict/lifecycle migration coverage."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from tests.unit.test_migration_0021 import _config, _match, _seed_v2


def test_0023_backfills_conflict_metadata_and_preserves_indexes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory-conflicts.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0022")
    with sqlite3.connect(path) as connection:
        _seed_v2(connection)
        connection.execute(
            """
            INSERT INTO groups (
                group_id, name, enabled, require_mention, autonomous_enabled,
                first_seen_at, last_seen_at, updated_at
            ) VALUES ('3001', '测试群', 1, 1, 1, '2026-08-01', '2026-08-01', '2026-08-01')
            """
        )
        connection.execute(
            """
            INSERT INTO memory_facts (
                id, scope_type, group_id, kind, memory_key, category,
                content, normalized_content, importance, confidence, source_type,
                status, created_at, updated_at
            ) VALUES (
                2, 'group', '3001', 'fact', 'group:topic', 'group',
                '群里常聊音乐', '群里常聊音乐', 3, 0.8, 'automatic', 'active',
                '2026-08-01', '2026-08-01'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO memory_evidence (
                fact_id, event_id, source_speaker_user_id, relation, excerpt, created_at
            ) VALUES (2, 1, '1001', 'self_statement', '群里常聊音乐', '2026-08-01')
            """
        )
        connection.execute(
            """
            INSERT INTO memory_embedding_profiles (
                id, fingerprint, provider_id, model_id, dimensions, output_type,
                document_template_version, endpoint_identity, created_at
            ) VALUES (1, ?, 'qwen_dashscope', 'test', 1, 'dense', 1, 'test', '2026-08-01')
            """,
            ("a" * 64,),
        )
        connection.execute(
            """
            INSERT INTO memory_embeddings (
                fact_id, profile_id, content_hash, vector_blob, created_at, updated_at
            ) VALUES (1, 1, ?, ?, '2026-08-01', '2026-08-01')
            """,
            ("b" * 64, b"\x00\x00\x00\x00"),
        )
        connection.execute(
            """
            INSERT INTO memory_embedding_jobs (
                fact_id, profile_id, content_hash, status, attempts,
                next_attempt_at, created_at, updated_at
            ) VALUES (2, 1, ?, 'pending', 0, '2026-08-01', '2026-08-01', '2026-08-01')
            """,
            ("c" * 64,),
        )
        connection.commit()

    command.upgrade(config, "0023")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0023",)
        fact = connection.execute(
            "SELECT authority, conflict_state, last_confirmed_at, invalidated_reason "
            "FROM memory_facts WHERE id = 1"
        ).fetchone()
        assert fact == ("self_report", "clear", "2026-08-01", None)
        evidence = connection.execute(
            "SELECT confidence, authority FROM memory_evidence WHERE fact_id = 1"
        ).fetchone()
        assert evidence == (0.9, "self_report")
        assert connection.execute("SELECT authority FROM memory_facts WHERE id = 2").fetchone() == (
            "group_report",
        )
        assert connection.execute(
            "SELECT confidence, authority FROM memory_evidence WHERE fact_id = 2"
        ).fetchone() == (0.7, "group_report")
        assert _match(connection, "准备考研") == (1,)
        assert _match(connection, "群里常聊音乐") == (2,)
        assert connection.execute("SELECT COUNT(*) FROM memory_embedding_profiles").fetchone() == (
            1,
        )
        assert connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM memory_embedding_jobs").fetchone() == (1,)
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {"memory_fact_relations", "memory_fact_state_events"} <= tables

        connection.execute(
            "UPDATE memory_facts SET status = 'contested', conflict_state = 'contested' "
            "WHERE id = 1"
        )
        connection.commit()

    with pytest.raises(RuntimeError, match="contested facts"):
        command.downgrade(config, "0022")

    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE memory_facts SET status = 'active', conflict_state = 'clear' WHERE id = 1"
        )
        connection.execute(
            "UPDATE memory_evidence SET relation = 'third_party_statement' WHERE fact_id = 2"
        )
        connection.commit()
    command.downgrade(config, "0022")
    with sqlite3.connect(path) as connection:
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(memory_facts)")}
        assert "authority" not in columns
        assert "conflict_state" not in columns
        assert "last_confirmed_at" not in columns
        assert "invalidated_reason" not in columns
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert "memory_fact_relations" not in tables
        assert "memory_fact_state_events" not in tables
        assert _match(connection, "准备考研") == (1,)
        assert connection.execute(
            "SELECT relation FROM memory_evidence WHERE fact_id = 2"
        ).fetchone() == ("self_statement",)
        assert connection.execute("SELECT COUNT(*) FROM memory_embeddings").fetchone() == (1,)


def test_0023_backfills_explicit_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory-explicit.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0022")
    with sqlite3.connect(path) as connection:
        _seed_v2(connection)
        connection.execute("UPDATE memory_facts SET source_type = 'explicit' WHERE id = 1")
        connection.execute(
            "UPDATE memory_evidence SET relation = 'explicit_command' WHERE fact_id = 1"
        )
        connection.commit()

    command.upgrade(config, "0023")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT authority FROM memory_facts WHERE id = 1").fetchone() == (
            "explicit",
        )
        assert connection.execute(
            "SELECT confidence, authority FROM memory_evidence WHERE fact_id = 1"
        ).fetchone() == (1.0, "explicit")
