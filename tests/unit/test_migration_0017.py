from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    return Config("alembic.ini")


def test_0017_adds_planner_voice_governance_without_losing_people(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "planner-voice.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0016")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO people (
                user_id, nickname, enabled, is_bot, first_seen_at, last_seen_at
            ) VALUES ('10001', '保留用户', 1, 0, '2026-07-29', '2026-07-29')
            """
        )
        connection.execute(
            """
            INSERT INTO chat_events (
                bot_user_id, platform_message_id, scope_type, private_peer_user_id,
                sender_user_id, direction, content, segments_json,
                occurred_at, observed_at, visual_summary, origin
            ) VALUES (
                '8000', 'legacy-voice', 'private', '10001', '8000', 'outbound',
                '[语音：Yuki 发送了一条语音，声线：roxy，风格：happy，语言：jp]',
                '[{"type":"record","data":{"profile_id":"roxy"}}]',
                '2026-07-29', '2026-07-29', '', 'user_message'
            )
            """
        )
        connection.commit()

    command.upgrade(config, "0017")
    with sqlite3.connect(path) as connection:
        planner_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(planner_runs)")
        }
        preference_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(person_speech_preferences)")
        }
        person = connection.execute(
            "SELECT nickname FROM people WHERE user_id = '10001'"
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        voice_content = connection.execute(
            "SELECT content FROM chat_events WHERE platform_message_id = 'legacy-voice'"
        ).fetchone()

    assert {
        "voice_mode",
        "voice_intent",
        "voice_tool_policy",
        "voice_reason",
        "voice_preference_change",
        "spontaneous_frequency",
        "recent_voice_ratio",
    } <= planner_columns
    assert {"user_id", "mode", "source_message_id", "created_at", "updated_at"} <= (
        preference_columns
    )
    assert person == ("保留用户",)
    assert voice_content == ("",)
    assert revision == ("0017",)

    command.downgrade(config, "0016")
    with sqlite3.connect(path) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        downgraded_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(planner_runs)")
        }
        downgraded_revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()

    assert "person_speech_preferences" not in tables
    assert "voice_intent" not in downgraded_columns
    assert downgraded_revision == ("0016",)
