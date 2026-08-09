"""Long-Episode SELF reflection starts at an explicit deployment baseline."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config


def test_0031_preserves_memory_and_chat_but_resets_pending_reflection(
    tmp_path: Path,
) -> None:
    database = tmp_path / "migration.db"
    config = Config("alembic.ini")
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{database.as_posix()}"
    try:
        command.upgrade(config, "0030")
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO people "
                "(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
                "VALUES ('1001','user',1,0,'2026-08-09','2026-08-09')"
            )
            connection.execute(
                "INSERT INTO people "
                "(user_id,nickname,enabled,is_bot,first_seen_at,last_seen_at) "
                "VALUES ('8000','Yuki',1,1,'2026-08-09','2026-08-09')"
            )
            connection.execute(
                """
                INSERT INTO chat_events (
                    bot_user_id, platform_message_id, scope_type, private_peer_user_id,
                    sender_user_id, direction, content, visual_summary, segments_json,
                    origin, occurred_at, observed_at
                ) VALUES (
                    '8000', 'baseline-event', 'private', '1001', '1001', 'inbound',
                    '仍然保留的聊天', '', '[]', 'user_message',
                    '2026-08-09', '2026-08-09'
                )
                """
            )
            event_id = int(connection.execute("SELECT MAX(id) FROM chat_events").fetchone()[0])
            connection.execute(
                "INSERT INTO memory_self_reflection_runtime "
                "(id,last_scanned_event_id,updated_at) VALUES (1,0,'2026-08-09')"
            )
            connection.execute(
                """
                INSERT INTO memory_self_reflection_states (
                    conversation_key_hash, bot_user_id, scope_type,
                    private_peer_user_id, last_event_id, latest_event_id,
                    pending_events, pending_characters, pending_since,
                    has_yuki_reply, has_tool_result, high_value_signal, updated_at
                ) VALUES (
                    'legacy-pending', '8000', 'private', '1001', 0, ?, 1, 8,
                    '2026-08-09', 1, 0, 0, '2026-08-09'
                )
                """,
                (event_id,),
            )
            connection.commit()
        command.upgrade(config, "0031")
    finally:
        if previous is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = previous

    with sqlite3.connect(database) as connection:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        event_count = connection.execute("SELECT COUNT(*) FROM chat_events").fetchone()
        state_count = connection.execute(
            "SELECT COUNT(*) FROM memory_self_reflection_states"
        ).fetchone()
        cursor = connection.execute(
            "SELECT last_scanned_event_id FROM memory_self_reflection_runtime WHERE id=1"
        ).fetchone()

    assert revision == ("0031",)
    assert event_count == (1,)
    assert state_count == (0,)
    assert cursor == (event_id,)
