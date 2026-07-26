"""Add expiring structured visual analysis cache.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the cache without persisting images, Base64, or temporary paths."""

    # Be defensive for development databases created directly from ORM metadata
    # and prerelease 1.4 builds that may already contain this table. Historical
    # revision 0005 explicitly excludes it, so normal fresh installs create it here.
    if "media_analyses" in inspect(op.get_bind()).get_table_names():
        return

    op.create_table(
        "media_analyses",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("source_event_id", sa.Integer(), nullable=True),
        sa.Column("segment_index", sa.Integer(), server_default="0", nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("analysis_mode", sa.String(length=16), nullable=False),
        sa.Column("question_hash", sa.String(length=64), server_default="", nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=64), nullable=False),
        sa.Column("observation_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "analysis_mode IN ('general', 'meme', 'ocr', 'question')",
            name="ck_media_analyses_analysis_mode",
        ),
        sa.CheckConstraint(
            "segment_index >= 0",
            name="ck_media_analyses_segment_index",
        ),
        sa.ForeignKeyConstraint(
            ["source_event_id"],
            ["chat_events.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "content_hash",
            "analysis_mode",
            "question_hash",
            "model",
            "prompt_version",
            name="uq_media_analyses_cache_key",
        ),
    )
    op.create_index(
        "ix_media_analyses_content_hash",
        "media_analyses",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        "ix_media_analyses_source_event_segment",
        "media_analyses",
        ["source_event_id", "segment_index"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only the visual analysis cache."""

    op.drop_index(
        "ix_media_analyses_source_event_segment",
        table_name="media_analyses",
    )
    op.drop_index("ix_media_analyses_content_hash", table_name="media_analyses")
    op.drop_table("media_analyses")
