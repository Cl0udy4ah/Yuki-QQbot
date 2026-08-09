"""Reset SELF reflection scanning at the long-episode rollout boundary.

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Episode reflection deliberately starts at deployment. Existing facts, evidence,
    # runs, and chat events stay intact; only unprocessed reflection cursors are reset.
    op.execute("DELETE FROM memory_self_reflection_states")
    with op.batch_alter_table("memory_self_reflection_states", recreate="always") as batch:
        batch.drop_constraint("uq_memory_self_reflection_state_key", type_="unique")
        batch.create_unique_constraint(
            "uq_memory_self_reflection_state_key_bot",
            ["conversation_key_hash", "bot_user_id"],
        )
    with op.batch_alter_table("memory_self_reflection_runs", recreate="always") as batch:
        batch.drop_constraint("uq_self_reflection_run_slot", type_="unique")
        batch.add_column(
            sa.Column(
                "bot_user_id",
                sa.String(length=64),
                nullable=False,
                server_default="",
            )
        )
        batch.create_unique_constraint(
            "uq_self_reflection_run_slot_bot",
            ["conversation_key_hash", "bot_user_id", "scheduled_slot"],
        )
    op.execute(
        """
        INSERT OR IGNORE INTO memory_self_reflection_runtime (
            id, last_scanned_event_id, updated_at
        ) VALUES (
            1, COALESCE((SELECT MAX(id) FROM chat_events), 0), CURRENT_TIMESTAMP
        )
        """
    )
    op.execute(
        """
        UPDATE memory_self_reflection_runtime
        SET last_scanned_event_id = COALESCE((SELECT MAX(id) FROM chat_events), 0),
            updated_at = CURRENT_TIMESTAMP
        WHERE id = 1
        """
    )


def downgrade() -> None:
    with op.batch_alter_table("memory_self_reflection_runs", recreate="always") as batch:
        batch.drop_constraint("uq_self_reflection_run_slot_bot", type_="unique")
        batch.create_unique_constraint(
            "uq_self_reflection_run_slot",
            ["conversation_key_hash", "scheduled_slot"],
        )
        batch.drop_column("bot_user_id")
    with op.batch_alter_table("memory_self_reflection_states", recreate="always") as batch:
        batch.drop_constraint("uq_memory_self_reflection_state_key_bot", type_="unique")
        batch.create_unique_constraint(
            "uq_memory_self_reflection_state_key",
            ["conversation_key_hash"],
        )
    # The previous cursor cannot be reconstructed without replaying private history.
    # Keeping the deployment baseline is the safe, non-replaying downgrade behavior.
