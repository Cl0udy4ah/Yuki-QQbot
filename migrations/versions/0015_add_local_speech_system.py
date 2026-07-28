"""Add the fully local speech profile and generation ledger.

Revision ID: 0015
Revises: 0014
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "speech_voice_profiles",
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("engine_model_version", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("model_relative_path", sa.String(length=512), nullable=False),
        sa.Column("model_checksum", sa.String(length=64), nullable=False),
        sa.Column("default_style", sa.String(length=128), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("source", sa.String(length=64), nullable=False),
        sa.Column("source_note", sa.Text(), server_default="", nullable=False),
        sa.Column("license_note", sa.Text(), server_default="", nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("provider = 'genie'", name="ck_speech_profiles_provider"),
        sa.CheckConstraint(
            "engine_model_version IN ('v2', 'v2proplus')",
            name="ck_speech_profiles_model_version",
        ),
        sa.PrimaryKeyConstraint("profile_id"),
    )
    op.create_index(
        "uq_speech_profiles_one_default",
        "speech_voice_profiles",
        ["is_default"],
        unique=True,
        sqlite_where=sa.text("is_default = 1 AND enabled = 1"),
    )
    op.create_index(
        "ix_speech_profiles_enabled_updated",
        "speech_voice_profiles",
        ["enabled", "updated_at"],
    )

    op.create_table(
        "speech_voice_references",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("reference_key", sa.String(length=128), nullable=False),
        sa.Column("style", sa.String(length=128), nullable=False),
        sa.Column("aliases_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("audio_relative_path", sa.String(length=512), nullable=False),
        sa.Column("audio_checksum", sa.String(length=64), nullable=False),
        sa.Column("transcript", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["speech_voice_profiles.profile_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("profile_id", "reference_key", name="uq_speech_references_profile_key"),
    )
    op.create_index(
        "ix_speech_references_profile_enabled",
        "speech_voice_references",
        ["profile_id", "enabled", "priority"],
    )

    op.create_table(
        "speech_generations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_key_hash", sa.String(length=64), nullable=False),
        sa.Column("trigger_event_id", sa.Integer(), nullable=True),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("reference_id", sa.Integer(), nullable=True),
        sa.Column("engine_version", sa.String(length=32), nullable=False),
        sa.Column("text_hash", sa.String(length=64), nullable=False),
        sa.Column("normalized_text_hash", sa.String(length=64), nullable=False),
        sa.Column("character_count", sa.Integer(), nullable=False),
        sa.Column("cache_key", sa.String(length=64), nullable=False),
        sa.Column("output_relative_path", sa.String(length=512), server_default="", nullable=False),
        sa.Column("output_format", sa.String(length=16), server_default="wav", nullable=False),
        sa.Column("sample_rate", sa.Integer(), nullable=True),
        sa.Column("channels", sa.Integer(), nullable=True),
        sa.Column("duration_milliseconds", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("character_count > 0", name="ck_speech_generations_character_count"),
        sa.CheckConstraint(
            "status IN ('queued', 'generating', 'succeeded', 'failed', 'cancelled', "
            "'sent', 'expired')",
            name="ck_speech_generations_status",
        ),
        sa.ForeignKeyConstraint(["trigger_event_id"], ["chat_events.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["speech_voice_profiles.profile_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["reference_id"], ["speech_voice_references.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", name="uq_speech_generations_request_id"),
    )
    op.create_index("ix_speech_generations_cache_key", "speech_generations", ["cache_key"])
    op.create_index(
        "ix_speech_generations_status_created",
        "speech_generations",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_speech_generations_expires",
        "speech_generations",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_speech_generations_expires", table_name="speech_generations")
    op.drop_index("ix_speech_generations_status_created", table_name="speech_generations")
    op.drop_index("ix_speech_generations_cache_key", table_name="speech_generations")
    op.drop_table("speech_generations")
    op.drop_index("ix_speech_references_profile_enabled", table_name="speech_voice_references")
    op.drop_table("speech_voice_references")
    op.drop_index("ix_speech_profiles_enabled_updated", table_name="speech_voice_profiles")
    op.drop_index("uq_speech_profiles_one_default", table_name="speech_voice_profiles")
    op.drop_table("speech_voice_profiles")
