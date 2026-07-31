"""Memory V2 embedding-index migration preserves the relational source of truth."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from tests.unit.test_migration_0021 import _config, _seed_v2


def test_0022_preserves_v2_and_adds_empty_derived_tables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "memory-embedding.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0021")
    with sqlite3.connect(path) as connection:
        _seed_v2(connection)
        assert connection.execute("SELECT COUNT(*) FROM memory_facts_fts").fetchone() == (1,)

    command.upgrade(config, "head")
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0022",)
        assert connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM memory_evidence").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM memory_facts_fts").fetchone() == (1,)
        for table in (
            "memory_embedding_profiles",
            "memory_embeddings",
            "memory_embedding_jobs",
        ):
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone() == (0,)
        schema_text = "\n".join(
            str(row[0] or "")
            for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")
        )
        assert "API_KEY" not in schema_text.upper()
        assert "AUTHORIZATION" not in schema_text.upper()

    command.downgrade(config, "0021")
    with sqlite3.connect(path) as connection:
        names = {str(row[0]) for row in connection.execute("SELECT name FROM sqlite_master")}
        assert "memory_embedding_profiles" not in names
        assert "memory_embeddings" not in names
        assert "memory_embedding_jobs" not in names
        assert "memory_facts" in names
        assert "memory_evidence" in names
        assert "memory_facts_fts" in names
        assert connection.execute("SELECT COUNT(*) FROM memory_facts").fetchone() == (1,)
