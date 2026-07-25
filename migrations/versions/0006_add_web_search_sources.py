"""Add isolated persisted source metadata for controlled web search.

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create web search runs and display-safe source metadata."""

    op.create_table(
        "web_search_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_key", sa.String(length=255), nullable=False),
        sa.Column("trigger_message_id", sa.String(length=128), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("partial_failure", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_web_search_runs_conversation_created",
        "web_search_runs",
        ["conversation_key", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_web_search_runs_conversation_trigger",
        "web_search_runs",
        ["conversation_key", "trigger_message_id"],
        unique=False,
    )
    op.create_table(
        "web_search_sources",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("domain", sa.String(length=255), nullable=False),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_score", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["web_search_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "url", name="uq_web_search_sources_run_url"),
    )
    op.create_index(
        "ix_web_search_sources_run_ordinal",
        "web_search_sources",
        ["run_id", "ordinal"],
        unique=False,
    )


def downgrade() -> None:
    """Remove web source metadata without touching chat or memory tables."""

    op.drop_index("ix_web_search_sources_run_ordinal", table_name="web_search_sources")
    op.drop_table("web_search_sources")
    op.drop_index("ix_web_search_runs_conversation_trigger", table_name="web_search_runs")
    op.drop_index("ix_web_search_runs_conversation_created", table_name="web_search_runs")
    op.drop_table("web_search_runs")
