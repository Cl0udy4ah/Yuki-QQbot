"""Preserve immutable sender identity snapshots on chat events.

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("chat_events")}
    additions = (
        sa.Column("sender_nickname", sa.String(128), nullable=False, server_default=""),
        sa.Column("sender_group_card", sa.String(128), nullable=False, server_default=""),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("chat_events", column)


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("chat_events")}
    with op.batch_alter_table("chat_events") as batch:
        if "sender_group_card" in columns:
            batch.drop_column("sender_group_card")
        if "sender_nickname" in columns:
            batch.drop_column("sender_nickname")
