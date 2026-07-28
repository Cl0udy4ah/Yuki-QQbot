"""Add persistent emoji collection and reply-effect storage.

Revision ID: 0014
Revises: 0013
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create emoji tables without changing existing media or chat data."""

    op.create_table(
        "emoji_assets",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("perceptual_hash", sa.String(length=64), nullable=True),
        sa.Column("relative_path", sa.String(length=512), nullable=False),
        sa.Column("preview_relative_path", sa.String(length=512), nullable=True),
        sa.Column("image_format", sa.String(length=16), nullable=False),
        sa.Column("mime_type", sa.String(length=64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("frame_count", sa.Integer(), nullable=False),
        sa.Column("animated", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="candidate", nullable=False),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("emotion_tags_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("usage_scenarios_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("ocr_text", sa.Text(), server_default="", nullable=False),
        sa.Column("intensity", sa.Float(), server_default="0.5", nullable=False),
        sa.Column("confidence", sa.Float(), server_default="0", nullable=False),
        sa.Column("analysis_version", sa.String(length=64), server_default="", nullable=False),
        sa.Column("pinned", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("source_event_id", sa.Integer(), nullable=True),
        sa.Column("first_seen_user_id", sa.String(length=64), nullable=True),
        sa.Column("first_seen_group_id", sa.String(length=64), nullable=True),
        sa.Column("source_sub_type", sa.String(length=64), server_default="", nullable=False),
        sa.Column("source_emoji_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("source_package_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("seen_count", sa.Integer(), server_default="1", nullable=False),
        sa.Column("use_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("missing_since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("byte_size > 0", name="ck_emoji_assets_byte_size"),
        sa.CheckConstraint("width > 0 AND height > 0", name="ck_emoji_assets_dimensions"),
        sa.CheckConstraint("frame_count > 0", name="ck_emoji_assets_frame_count"),
        sa.CheckConstraint("seen_count >= 1", name="ck_emoji_assets_seen_count"),
        sa.CheckConstraint("use_count >= 0", name="ck_emoji_assets_use_count"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_emoji_assets_confidence"
        ),
        sa.CheckConstraint("intensity >= 0 AND intensity <= 1", name="ck_emoji_assets_intensity"),
        sa.CheckConstraint(
            "status IN ('candidate', 'recognized', 'adopted', 'rejected', 'banned', 'missing')",
            name="ck_emoji_assets_status",
        ),
        sa.ForeignKeyConstraint(["first_seen_group_id"], ["groups.group_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["first_seen_user_id"], ["people.user_id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["source_event_id"], ["chat_events.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("relative_path", name="uq_emoji_assets_relative_path"),
        sa.UniqueConstraint("sha256", name="uq_emoji_assets_sha256"),
    )
    op.create_index("ix_emoji_assets_last_seen", "emoji_assets", ["last_seen_at"])
    op.create_index("ix_emoji_assets_perceptual_hash", "emoji_assets", ["perceptual_hash"])
    op.create_index("ix_emoji_assets_status_updated", "emoji_assets", ["status", "updated_at"])

    op.create_table(
        "emoji_scope_states",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("emoji_id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("weight", sa.Float(), server_default="1", nullable=False),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("scope_type IN ('global', 'group')", name="ck_emoji_scope_scope_type"),
        sa.CheckConstraint(
            "(scope_type = 'global' AND scope_id = '') OR "
            "(scope_type = 'group' AND scope_id <> '')",
            name="ck_emoji_scope_scope_id",
        ),
        sa.CheckConstraint("weight >= 0", name="ck_emoji_scope_weight"),
        sa.ForeignKeyConstraint(["emoji_id"], ["emoji_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("emoji_id", "scope_type", "scope_id", name="uq_emoji_scope_state"),
    )
    op.create_index(
        "ix_emoji_scope_lookup", "emoji_scope_states", ["scope_type", "scope_id", "enabled"]
    )

    op.create_table(
        "emoji_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("emoji_id", sa.String(length=36), nullable=False),
        sa.Column("job_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("claimed_by", sa.String(length=64), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempts >= 0", name="ck_emoji_jobs_attempts"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed')",
            name="ck_emoji_jobs_status",
        ),
        sa.CheckConstraint(
            "job_type IN ('analyze', 'reanalyze', 'rebuild_preview')",
            name="ck_emoji_jobs_type",
        ),
        sa.ForeignKeyConstraint(["emoji_id"], ["emoji_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_emoji_jobs_status_next", "emoji_jobs", ["status", "next_attempt_at"])
    op.create_index(
        "uq_emoji_jobs_active",
        "emoji_jobs",
        ["emoji_id", "job_type"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'processing')"),
    )

    op.create_table(
        "emoji_usage_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("emoji_id", sa.String(length=36), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("group_id", sa.String(length=64), nullable=True),
        sa.Column("trigger_message_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["emoji_id"], ["emoji_assets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_emoji_usage_asset_created", "emoji_usage_events", ["emoji_id", "created_at"]
    )
    op.create_index(
        "ix_emoji_usage_scope_created", "emoji_usage_events", ["group_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_emoji_usage_scope_created", table_name="emoji_usage_events")
    op.drop_index("ix_emoji_usage_asset_created", table_name="emoji_usage_events")
    op.drop_table("emoji_usage_events")
    op.drop_index("uq_emoji_jobs_active", table_name="emoji_jobs")
    op.drop_index("ix_emoji_jobs_status_next", table_name="emoji_jobs")
    op.drop_table("emoji_jobs")
    op.drop_index("ix_emoji_scope_lookup", table_name="emoji_scope_states")
    op.drop_table("emoji_scope_states")
    op.drop_index("ix_emoji_assets_status_updated", table_name="emoji_assets")
    op.drop_index("ix_emoji_assets_perceptual_hash", table_name="emoji_assets")
    op.drop_index("ix_emoji_assets_last_seen", table_name="emoji_assets")
    op.drop_table("emoji_assets")
