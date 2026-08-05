"""Add plugin external events, target grants, artifacts, Outbox, and turns.

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_VALID = """
(
    NEW.event_kind = 'message'
    AND NEW.source_plugin_id IS NULL
    AND NEW.external_source IS NULL
    AND NEW.external_event_key IS NULL
    AND NEW.external_event_type IS NULL
    AND NEW.external_payload_json IS NULL
    AND NEW.external_target_id IS NULL
) OR (
    NEW.event_kind = 'external_event'
    AND NEW.source_plugin_id IS NOT NULL
    AND NEW.external_source IS NOT NULL
    AND NEW.external_event_key IS NOT NULL
    AND NEW.external_event_type IS NOT NULL
    AND NEW.external_payload_json IS NOT NULL
    AND NEW.external_target_id IS NOT NULL
    AND NEW.origin = 'plugin_background'
    AND NEW.direction = 'external'
)
"""


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    columns = {column["name"] for column in inspector.get_columns("chat_events")}
    additions = (
        sa.Column("event_kind", sa.String(32), nullable=False, server_default="message"),
        sa.Column("source_plugin_id", sa.String(128)),
        sa.Column("external_source", sa.String(64)),
        sa.Column("external_event_key", sa.String(255)),
        sa.Column("external_event_type", sa.String(128)),
        sa.Column("external_payload_json", sa.Text()),
        sa.Column("external_target_id", sa.String(64)),
    )
    for column in additions:
        if column.name not in columns:
            op.add_column("chat_events", column)
    indexes = {index["name"] for index in inspector.get_indexes("chat_events")}
    if "uq_chat_events_external_event_target" not in indexes:
        op.create_index(
            "uq_chat_events_external_event_target",
            "chat_events",
            ["source_plugin_id", "external_event_key", "scope_type", "external_target_id"],
            unique=True,
            sqlite_where=sa.text("event_kind = 'external_event'"),
        )
    op.execute(
        f"""
        CREATE TRIGGER ck_chat_events_kind_payload_insert
        BEFORE INSERT ON chat_events
        WHEN NOT ({_EVENT_VALID})
        BEGIN
            SELECT RAISE(ABORT, 'invalid chat event kind payload');
        END
        """
    )
    op.execute(
        f"""
        CREATE TRIGGER ck_chat_events_kind_payload_update
        BEFORE UPDATE OF event_kind, source_plugin_id, external_source,
            external_event_key, external_event_type, external_payload_json,
            external_target_id, origin, direction
        ON chat_events
        WHEN NOT ({_EVENT_VALID})
        BEGIN
            SELECT RAISE(ABORT, 'invalid chat event kind payload');
        END
        """
    )

    op.create_table(
        "plugin_background_target_grants",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("bot_user_id", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_user_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugin_installations.plugin_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["people.user_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint(
            "plugin_id", "target_type", "target_id", name="uq_plugin_background_target"
        ),
        sa.CheckConstraint(
            "target_type IN ('group','private')", name="ck_plugin_background_target_type"
        ),
    )
    op.create_index(
        "ix_plugin_background_target_enabled",
        "plugin_background_target_grants",
        ["plugin_id", "enabled"],
    )

    op.create_table(
        "plugin_media_artifacts",
        sa.Column("handle_id", sa.String(128), primary_key=True),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("content_type", sa.String(128), nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugin_installations.plugin_id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint("byte_size > 0", name="ck_plugin_media_artifacts_size"),
    )
    op.create_index(
        "ix_plugin_media_artifacts_plugin_expires",
        "plugin_media_artifacts",
        ["plugin_id", "expires_at"],
    )
    op.create_index(
        "ix_plugin_media_artifacts_sha",
        "plugin_media_artifacts",
        ["plugin_id", "sha256"],
    )

    op.create_table(
        "plugin_notification_outbox",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("notification_id", sa.String(64), nullable=False),
        sa.Column("part_key", sa.String(255), nullable=False),
        sa.Column("source_event_id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("bot_user_id", sa.String(64), nullable=False),
        sa.Column("part_type", sa.String(16), nullable=False),
        sa.Column("text", sa.Text(), nullable=False, server_default=""),
        sa.Column("media_handle_id", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("platform_message_id", sa.String(128)),
        sa.Column("last_error_category", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["source_event_id"], ["chat_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugin_installations.plugin_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["media_handle_id"], ["plugin_media_artifacts.handle_id"], ondelete="SET NULL"
        ),
        sa.UniqueConstraint(
            "notification_id", "part_key", name="uq_plugin_notification_outbox_part"
        ),
        sa.CheckConstraint(
            "target_type IN ('group','private')", name="ck_plugin_outbox_target_type"
        ),
        sa.CheckConstraint(
            "part_type IN ('text','media','agent_reply')",
            name="ck_plugin_notification_outbox_part_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','processing','sent','failed','uncertain','cancelled')",
            name="ck_plugin_notification_outbox_status",
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_plugin_outbox_attempts"),
    )
    for name, columns in (
        ("ix_plugin_notification_outbox_due", ["status", "next_attempt_at"]),
        ("ix_plugin_notification_outbox_plugin", ["plugin_id", "status"]),
        ("ix_plugin_notification_outbox_source", ["source_event_id"]),
    ):
        op.create_index(name, "plugin_notification_outbox", columns)

    op.create_table(
        "plugin_background_turn_jobs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("source_event_id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.String(128), nullable=False),
        sa.Column("target_type", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("bot_user_id", sa.String(64), nullable=False),
        sa.Column("agent_intent", sa.String(1000), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("generated_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("model_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_category", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(["source_event_id"], ["chat_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugin_installations.plugin_id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("source_event_id", name="uq_plugin_background_turn_source"),
        sa.CheckConstraint("target_type IN ('group','private')", name="ck_plugin_turn_target_type"),
        sa.CheckConstraint(
            "status IN ('pending','processing','completed','failed','cancelled')",
            name="ck_plugin_background_turn_status",
        ),
        sa.CheckConstraint("attempts >= 0 AND max_attempts >= 1", name="ck_plugin_turn_attempts"),
    )
    op.create_index(
        "ix_plugin_background_turn_due",
        "plugin_background_turn_jobs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_plugin_background_turn_plugin",
        "plugin_background_turn_jobs",
        ["plugin_id", "status"],
    )


def downgrade() -> None:
    op.drop_table("plugin_background_turn_jobs")
    op.drop_table("plugin_notification_outbox")
    op.drop_table("plugin_media_artifacts")
    op.drop_table("plugin_background_target_grants")
    op.execute("DROP TRIGGER IF EXISTS ck_chat_events_kind_payload_update")
    op.execute("DROP TRIGGER IF EXISTS ck_chat_events_kind_payload_insert")
    inspector = inspect(op.get_bind())
    indexes = {index["name"] for index in inspector.get_indexes("chat_events")}
    if "uq_chat_events_external_event_target" in indexes:
        op.drop_index("uq_chat_events_external_event_target", table_name="chat_events")
    checks = {constraint["name"] for constraint in inspector.get_check_constraints("chat_events")}
    with op.batch_alter_table("chat_events") as batch:
        if "ck_chat_events_kind_payload" in checks:
            batch.drop_constraint("ck_chat_events_kind_payload", type_="check")
        batch.drop_column("external_target_id")
        batch.drop_column("external_payload_json")
        batch.drop_column("external_event_type")
        batch.drop_column("external_event_key")
        batch.drop_column("external_source")
        batch.drop_column("source_plugin_id")
        batch.drop_column("event_kind")
