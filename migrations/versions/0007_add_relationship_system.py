"""Add persistent affection and trust relationships.

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create relationship state, audit events, and restart-safe jobs."""

    op.create_table(
        "person_relationships",
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("affection_score", sa.Integer(), server_default="50", nullable=False),
        sa.Column("trust_score", sa.Integer(), server_default="50", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_automatic_change_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "affection_score >= 0 AND affection_score <= 100",
            name="ck_person_relationships_affection_range",
        ),
        sa.CheckConstraint(
            "trust_score >= 0 AND trust_score <= 100",
            name="ck_person_relationships_trust_range",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )
    op.execute(
        sa.text(
            """
            INSERT INTO person_relationships (
                user_id, affection_score, trust_score, created_at, updated_at
            )
            SELECT user_id, 50, 50, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            FROM people
            WHERE is_bot = 0
            """
        )
    )

    op.create_table(
        "relationship_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("source_event_id", sa.Integer(), nullable=True),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("change_type", sa.String(length=16), nullable=False),
        sa.Column("affection_before", sa.Integer(), nullable=False),
        sa.Column("affection_delta", sa.Integer(), nullable=False),
        sa.Column("affection_after", sa.Integer(), nullable=False),
        sa.Column("trust_before", sa.Integer(), nullable=False),
        sa.Column("trust_delta", sa.Integer(), nullable=False),
        sa.Column("trust_after", sa.Integer(), nullable=False),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "change_type IN ('automatic', 'manual')",
            name="ck_relationship_events_change_type",
        ),
        sa.ForeignKeyConstraint(["source_event_id"], ["chat_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_relationship_events_user_created",
        "relationship_events",
        ["user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "uq_relationship_events_automatic_source",
        "relationship_events",
        ["source_event_id"],
        unique=True,
        sqlite_where=sa.text("source_event_id IS NOT NULL AND change_type = 'automatic'"),
    )

    op.create_table(
        "relationship_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("trigger_event_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_relationship_jobs_status",
        ),
        sa.ForeignKeyConstraint(["trigger_event_id"], ["chat_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "trigger_event_id",
            name="uq_relationship_jobs_trigger_event",
        ),
    )
    op.create_index(
        "ix_relationship_jobs_status_next",
        "relationship_jobs",
        ["status", "next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only the relationship subsystem."""

    op.drop_index("ix_relationship_jobs_status_next", table_name="relationship_jobs")
    op.drop_table("relationship_jobs")
    op.drop_index(
        "uq_relationship_events_automatic_source",
        table_name="relationship_events",
    )
    op.drop_index(
        "ix_relationship_events_user_created",
        table_name="relationship_events",
    )
    op.drop_table("relationship_events")
    op.drop_table("person_relationships")
