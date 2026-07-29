"""Make Planner the authoritative chat voice decision boundary.

Revision ID: 0017
Revises: 0016
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Older text-and-voice events duplicated structured TTS metadata into the
    # conversational content column. The record segment still retains profile,
    # style and generation identifiers, so blanking only this generated prose
    # prevents it from leaking back into future model context or memory jobs.
    op.execute(
        sa.text(
            """
            UPDATE chat_events
            SET content = ''
            WHERE direction = 'outbound'
              AND content LIKE '[语音：Yuki 发送了一条语音，声线：%'
            """
        )
    )
    op.create_table(
        "person_speech_preferences",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("mode", sa.String(length=32), nullable=False),
        sa.Column("source_message_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "mode IN ('text_only', 'auto', 'prefer_voice')",
            name="ck_person_speech_preferences_mode",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.create_index(
        "ix_person_speech_preferences_updated",
        "person_speech_preferences",
        ["updated_at"],
    )
    with op.batch_alter_table("planner_runs") as batch:
        batch.add_column(sa.Column("voice_mode", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("voice_intent", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("voice_tool_policy", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("voice_reason", sa.String(length=300), nullable=True))
        batch.add_column(sa.Column("voice_preference_change", sa.String(length=32), nullable=True))
        batch.add_column(sa.Column("spontaneous_frequency", sa.Float(), nullable=True))
        batch.add_column(sa.Column("recent_voice_ratio", sa.Float(), nullable=True))
        batch.create_check_constraint(
            "ck_planner_runs_spontaneous_frequency",
            "spontaneous_frequency IS NULL OR "
            "(spontaneous_frequency >= 0 AND spontaneous_frequency <= 1)",
        )
        batch.create_check_constraint(
            "ck_planner_runs_recent_voice_ratio",
            "recent_voice_ratio IS NULL OR (recent_voice_ratio >= 0 AND recent_voice_ratio <= 1)",
        )


def downgrade() -> None:
    with op.batch_alter_table("planner_runs") as batch:
        batch.drop_constraint("ck_planner_runs_recent_voice_ratio", type_="check")
        batch.drop_constraint("ck_planner_runs_spontaneous_frequency", type_="check")
        batch.drop_column("recent_voice_ratio")
        batch.drop_column("spontaneous_frequency")
        batch.drop_column("voice_preference_change")
        batch.drop_column("voice_reason")
        batch.drop_column("voice_tool_policy")
        batch.drop_column("voice_intent")
        batch.drop_column("voice_mode")
    op.drop_index(
        "ix_person_speech_preferences_updated",
        table_name="person_speech_preferences",
    )
    op.drop_table("person_speech_preferences")
