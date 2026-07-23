"""Add bounded group-scoped shared memories.

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the group memory table and group/update index."""

    op.create_table(
        "group_memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("group_id", sa.String(length=64), nullable=False),
        sa.Column("memory_key", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "group_id",
            "memory_key",
            name="uq_group_memories_group_key",
        ),
    )
    op.create_index(
        "ix_group_memories_group_updated",
        "group_memories",
        ["group_id", "updated_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop group memories in dependency order."""

    op.drop_index("ix_group_memories_group_updated", table_name="group_memories")
    op.drop_table("group_memories")
