"""Add persistent QQ emoji descriptions.

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the durable emoji lookup table without changing existing data."""

    if "emoji_descriptions" in inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "emoji_descriptions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("emoji_key", sa.String(length=255), nullable=False),
        sa.Column("analysis_mode", sa.String(length=16), nullable=False),
        sa.Column("question_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("observation_json", sa.Text(), nullable=False),
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "analysis_mode IN ('general', 'meme', 'ocr', 'question')",
            name="ck_emoji_descriptions_analysis_mode",
        ),
        sa.CheckConstraint("hit_count >= 0", name="ck_emoji_descriptions_hit_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "emoji_key",
            "analysis_mode",
            "question_hash",
            "model",
            "prompt_version",
            name="uq_emoji_descriptions_lookup",
        ),
    )
    op.create_index(
        "ix_emoji_descriptions_key",
        "emoji_descriptions",
        ["emoji_key"],
        unique=False,
    )
    op.create_index(
        "ix_emoji_descriptions_last_used",
        "emoji_descriptions",
        ["last_used_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only the reusable emoji description library."""

    if "emoji_descriptions" in inspect(op.get_bind()).get_table_names():
        op.drop_table("emoji_descriptions")
