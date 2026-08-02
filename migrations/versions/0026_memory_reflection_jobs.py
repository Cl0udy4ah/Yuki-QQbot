"""Add restart-safe bounded Memory V2 reflection jobs.

Revision ID: 0026
Revises: 0025
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0026"
down_revision: str | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create reflection task state only; startup does not scan or mutate memories."""

    op.create_table(
        "memory_reflection_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("issue_type", sa.String(24), nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("related_fact_id", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["related_fact_id"], ["memory_facts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("fingerprint", name="uq_memory_reflection_jobs_fingerprint"),
        sa.CheckConstraint(
            "issue_type IN ('duplicate','contested','attribution')",
            name="ck_memory_reflection_jobs_issue_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed')",
            name="ck_memory_reflection_jobs_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND max_attempts BETWEEN 1 AND 20",
            name="ck_memory_reflection_jobs_attempts",
        ),
    )
    op.create_index(
        "ix_memory_reflection_jobs_status_next",
        "memory_reflection_jobs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_memory_reflection_jobs_fact_issue",
        "memory_reflection_jobs",
        ["fact_id", "issue_type"],
    )


def downgrade() -> None:
    """Drop task state only; completed memory mutations remain fully audited."""

    op.drop_table("memory_reflection_jobs")
