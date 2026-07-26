"""Persist compact visual summaries on their source chat events.

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add a text-only derived summary without changing raw event content."""

    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("chat_events")}
    if "visual_summary" not in columns:
        op.add_column(
            "chat_events",
            sa.Column("visual_summary", sa.Text(), server_default="", nullable=False),
        )


def downgrade() -> None:
    """Remove only derived visual summaries."""

    columns = {column["name"] for column in inspect(op.get_bind()).get_columns("chat_events")}
    if "visual_summary" in columns:
        op.drop_column("chat_events", "visual_summary")
