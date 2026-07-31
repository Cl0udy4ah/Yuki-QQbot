"""Irreversibly replace legacy memories with identity-safe Memory V2.

Revision ID: 0020
Revises: 0019
Create Date: 2026-07-31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Destroy all V1 memory data, then create an empty Memory V2 store."""

    existing = set(inspect(op.get_bind()).get_table_names())
    for table_name in (
        "memory_jobs",
        "person_preferences",
        "person_group_memories",
        "group_memories",
        "person_memories",
    ):
        if table_name in existing:
            op.drop_table(table_name)

    op.create_table(
        "memory_facts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("subject_user_id", sa.String(length=64), nullable=True),
        sa.Column("group_id", sa.String(length=64), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("memory_key", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("normalized_content", sa.Text(), nullable=False),
        sa.Column("importance", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("source_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("supersedes_id", sa.Integer(), nullable=True),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True),
        sa.Column("valid_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope_type IN ('person', 'person_group', 'group')",
            name="ck_memory_facts_scope_type",
        ),
        sa.CheckConstraint(
            "kind IN ('fact', 'preference', 'episode')",
            name="ck_memory_facts_kind",
        ),
        sa.CheckConstraint(
            "source_type IN ('automatic', 'explicit', 'rebuild')",
            name="ck_memory_facts_source_type",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded', 'invalidated')",
            name="ck_memory_facts_status",
        ),
        sa.CheckConstraint(
            "importance BETWEEN 1 AND 5",
            name="ck_memory_facts_importance",
        ),
        sa.CheckConstraint(
            "confidence BETWEEN 0 AND 1",
            name="ck_memory_facts_confidence",
        ),
        sa.CheckConstraint(
            "(scope_type = 'person' AND subject_user_id IS NOT NULL AND group_id IS NULL) OR "
            "(scope_type = 'person_group' AND subject_user_id IS NOT NULL "
            "AND group_id IS NOT NULL) OR "
            "(scope_type = 'group' AND subject_user_id IS NULL AND group_id IS NOT NULL)",
            name="ck_memory_facts_scope_identity",
        ),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["supersedes_id"], ["memory_facts.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "uq_memory_facts_active_person_key",
        "memory_facts",
        ["subject_user_id", "kind", "memory_key"],
        unique=True,
        sqlite_where=sa.text("status = 'active' AND scope_type = 'person'"),
    )
    op.create_index(
        "uq_memory_facts_active_person_group_key",
        "memory_facts",
        ["subject_user_id", "group_id", "kind", "memory_key"],
        unique=True,
        sqlite_where=sa.text("status = 'active' AND scope_type = 'person_group'"),
    )
    op.create_index(
        "uq_memory_facts_active_group_key",
        "memory_facts",
        ["group_id", "kind", "memory_key"],
        unique=True,
        sqlite_where=sa.text("status = 'active' AND scope_type = 'group'"),
    )
    op.create_index(
        "ix_memory_facts_scope_status_updated",
        "memory_facts",
        ["scope_type", "subject_user_id", "group_id", "status", "updated_at"],
    )

    op.create_table(
        "memory_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("source_speaker_user_id", sa.String(length=64), nullable=False),
        sa.Column("relation", sa.String(length=24), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "relation IN ('self_statement', 'explicit_command', 'correction', 'rebuild')",
            name="ck_memory_evidence_relation",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["chat_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["source_speaker_user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_id", "event_id", name="uq_memory_evidence_fact_event"),
    )
    op.create_index("ix_memory_evidence_fact", "memory_evidence", ["fact_id"])
    op.create_index("ix_memory_evidence_event", "memory_evidence", ["event_id"])

    op.create_table(
        "memory_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("conversation_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')",
            name="ck_memory_jobs_status",
        ),
        sa.ForeignKeyConstraint(["event_id"], ["chat_events.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id", name="uq_memory_jobs_event"),
    )
    op.create_index("ix_memory_jobs_status_next", "memory_jobs", ["status", "next_attempt_at"])
    op.create_index("ix_memory_jobs_conversation", "memory_jobs", ["conversation_key", "id"])


def downgrade() -> None:
    raise RuntimeError(
        "Memory V2 cutover is irreversible; restore the pre-upgrade database backup."
    )
