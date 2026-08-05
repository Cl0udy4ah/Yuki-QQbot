"""Migration coverage for external-event and notification infrastructure."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_0028_adds_external_event_and_delivery_tables(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    config = Config("alembic.ini")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
    try:
        command.upgrade(config, "0028")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous
    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_events)")}
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    assert {
        "event_kind",
        "source_plugin_id",
        "external_event_key",
        "external_payload_json",
    } <= columns
    assert {
        "plugin_background_target_grants",
        "plugin_media_artifacts",
        "plugin_notification_outbox",
        "plugin_background_turn_jobs",
    } <= tables
    assert revision == ("0028",)
