from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

_SPEECH_TABLES = {
    "speech_voice_profiles",
    "speech_voice_references",
    "speech_generations",
}


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    url = f"sqlite+aiosqlite:///{path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    return Config("alembic.ini")


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as connection:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }


def test_0015_non_destructively_adds_and_removes_speech_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "speech.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0014")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO people (
                user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at
            ) VALUES ('10001', '保留用户', 1, 0, '2026-07-29', '2026-07-29')
            """
        )
        connection.commit()

    command.upgrade(config, "0015")
    assert _SPEECH_TABLES <= _tables(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT nickname FROM people").fetchone() == ("保留用户",)
        assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == ("0015",)

    command.downgrade(config, "0014")
    assert not (_SPEECH_TABLES & _tables(path))
