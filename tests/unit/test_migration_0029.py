"""Migration coverage for immutable chat-event sender identity snapshots."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_0029_preserves_existing_events_and_adds_identity_snapshots(tmp_path: Path) -> None:
    database = tmp_path / "migration.db"
    config = Config("alembic.ini")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
    try:
        command.upgrade(config, "0028")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO people "
                "(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
                "VALUES ('1001','旧昵称',1,0,'2026-08-05','2026-08-05')"
            )
            connection.execute(
                "INSERT INTO people "
                "(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
                "VALUES ('8000','Yuki',1,1,'2026-08-05','2026-08-05')"
            )
            connection.execute(
                """
                INSERT INTO chat_events (
                    bot_user_id, platform_message_id, scope_type, private_peer_user_id,
                    sender_user_id, direction, content, visual_summary, segments_json,
                    origin, occurred_at, observed_at
                ) VALUES (
                    '8000', 'old-event', 'private', '1001', '1001', 'inbound',
                    '旧消息', '', '[]', 'user_message', '2026-08-05', '2026-08-05'
                )
                """
            )
            connection.commit()
        command.upgrade(config, "0029")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    with sqlite3.connect(database) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(chat_events)")}
        identity = connection.execute(
            "SELECT sender_nickname, sender_group_card FROM chat_events "
            "WHERE platform_message_id='old-event'"
        ).fetchone()
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()

    assert {"sender_nickname", "sender_group_card"} <= columns
    assert identity == ("", "")
    assert revision == ("0029",)
