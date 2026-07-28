from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _config(path: Path, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{path.as_posix()}")
    return Config("alembic.ini")


def test_0016_adds_bilingual_profile_and_generation_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "speech-languages.db"
    config = _config(path, monkeypatch)
    command.upgrade(config, "0015")
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            INSERT INTO speech_voice_profiles (
                profile_id, display_name, provider, engine_model_version, language,
                model_relative_path, model_checksum, default_style, enabled, is_default,
                source, source_note, license_note, manifest_hash, created_at, updated_at
            ) VALUES (
                'roxy', 'Roxy', 'genie', 'v2proplus', 'jp',
                'voices/roxy/model', ?, 'neutral', 1, 1,
                'user_supplied', '', '', ?, '2026-07-29', '2026-07-29'
            )
            """,
            ("a" * 64, "b" * 64),
        )
        connection.commit()

    command.upgrade(config, "0016")
    with sqlite3.connect(path) as connection:
        languages = connection.execute(
            "SELECT supported_languages_json FROM speech_voice_profiles WHERE profile_id = 'roxy'"
        ).fetchone()
        profile_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(speech_voice_profiles)")
        }
        generation_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(speech_generations)")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert languages == ('["jp"]',)
    assert "supported_languages_json" in profile_columns
    assert "target_language" in generation_columns
    assert revision == ("0016",)
