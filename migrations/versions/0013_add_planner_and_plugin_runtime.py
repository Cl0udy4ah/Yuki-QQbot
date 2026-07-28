"""Add Planner observability and Plugin API v1 persistence.

Revision ID: 0013
Revises: 0012
Create Date: 2026-07-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create non-destructive Planner, plugin, and isolated session storage."""

    op.create_table(
        "plugin_installations",
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("version", sa.String(length=64), nullable=False),
        sa.Column("plugin_api", sa.String(length=32), nullable=False),
        sa.Column("yuki_requires", sa.String(length=128), nullable=False),
        sa.Column("manifest_hash", sa.String(length=64), nullable=False),
        sa.Column("entrypoint", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("approved_permissions_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("requested_permissions_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("failure_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_category", sa.String(length=64), nullable=True),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("failure_count >= 0", name="ck_plugin_installations_failure_count"),
        sa.CheckConstraint(
            "status IN ('discovered', 'invalid', 'pending_approval', 'approved', "
            "'registered', 'starting', 'running', 'stopping', 'disabled', 'failed', "
            "'incompatible')",
            name="ck_plugin_installations_status",
        ),
        sa.PrimaryKeyConstraint("plugin_id"),
    )
    op.create_index(
        "ix_plugin_installations_status_enabled",
        "plugin_installations",
        ["status", "enabled"],
    )
    op.create_index("ix_plugin_installations_updated", "plugin_installations", ["updated_at"])

    op.create_table(
        "plugin_config_values",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('global', 'group', 'user')",
            name="ck_plugin_config_values_scope_type",
        ),
        sa.CheckConstraint(
            "(scope_type = 'global' AND scope_id = '') OR "
            "(scope_type IN ('group', 'user') AND scope_id <> '')",
            name="ck_plugin_config_values_scope_id",
        ),
        sa.CheckConstraint("version >= 1", name="ck_plugin_config_values_version"),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugin_installations.plugin_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "plugin_id",
            "scope_type",
            "scope_id",
            "key",
            name="uq_plugin_config_values_scope_key",
        ),
    )
    op.create_index(
        "ix_plugin_config_values_plugin_scope",
        "plugin_config_values",
        ["plugin_id", "scope_type", "scope_id"],
    )

    op.create_table(
        "plugin_state",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("namespace", sa.String(length=128), nullable=False),
        sa.Column("key", sa.String(length=128), nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("subject_user_id", sa.String(length=64), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version >= 1", name="ck_plugin_state_version"),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugin_installations.plugin_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["subject_user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plugin_id", "namespace", "key", name="uq_plugin_state_namespace_key"),
    )
    op.create_index("ix_plugin_state_plugin_namespace", "plugin_state", ["plugin_id", "namespace"])
    op.create_index("ix_plugin_state_expires", "plugin_state", ["expires_at"])
    op.create_index("ix_plugin_state_subject", "plugin_state", ["subject_user_id"])

    op.create_table(
        "plugin_audit_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=True),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("permission", sa.String(length=128), nullable=True),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("detail_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_plugin_audit_events_plugin_created",
        "plugin_audit_events",
        ["plugin_id", "created_at"],
    )
    op.create_index(
        "ix_plugin_audit_events_actor_created",
        "plugin_audit_events",
        ["actor_user_id", "created_at"],
    )

    op.create_table(
        "planner_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("conversation_key_hash", sa.String(length=64), nullable=False),
        sa.Column("trigger_message_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("sender_user_id_hash", sa.String(length=64), nullable=False),
        sa.Column("group_id_hash", sa.String(length=64), nullable=True),
        sa.Column("necessity_score", sa.Float(), nullable=False),
        sa.Column("necessity_reasons_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("gate_decision", sa.String(length=32), nullable=False),
        sa.Column("planner_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("planner_model", sa.String(length=128), server_default="", nullable=False),
        sa.Column("planner_decision", sa.String(length=32), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=True),
        sa.Column("delivery_mode", sa.String(length=32), nullable=True),
        sa.Column("desired_messages", sa.Integer(), nullable=True),
        sa.Column("tool_mode", sa.String(length=32), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("latency_seconds", sa.Float(), server_default="0", nullable=False),
        sa.Column("interrupted", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("messages_planned", sa.Integer(), server_default="0", nullable=False),
        sa.Column("messages_sent", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "necessity_score >= 0 AND necessity_score <= 100",
            name="ck_planner_runs_necessity_score",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_planner_runs_confidence",
        ),
        sa.CheckConstraint(
            "desired_messages IS NULL OR desired_messages >= 0",
            name="ck_planner_runs_desired_messages",
        ),
        sa.CheckConstraint("messages_planned >= 0", name="ck_planner_runs_messages_planned"),
        sa.CheckConstraint("messages_sent >= 0", name="ck_planner_runs_messages_sent"),
        sa.CheckConstraint("latency_seconds >= 0", name="ck_planner_runs_latency"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_planner_runs_created", "planner_runs", ["created_at"])
    op.create_index(
        "ix_planner_runs_conversation_created",
        "planner_runs",
        ["conversation_key_hash", "created_at"],
    )
    op.create_index("ix_planner_runs_finished", "planner_runs", ["finished_at"])

    op.create_table(
        "plugin_agent_sessions",
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("plugin_id", sa.String(length=128), nullable=False),
        sa.Column("owner_user_id", sa.String(length=64), nullable=True),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("name", sa.String(length=128), server_default="", nullable=False),
        sa.Column("model", sa.String(length=128), server_default="", nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("persistence", sa.String(length=16), server_default="durable", nullable=False),
        sa.Column("context_profile", sa.String(length=32), server_default="none", nullable=False),
        sa.Column("allowed_capabilities_json", sa.Text(), server_default="[]", nullable=False),
        sa.Column("status", sa.String(length=16), server_default="active", nullable=False),
        sa.Column("next_sequence", sa.Integer(), server_default="1", nullable=False),
        sa.Column("turn_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "scope_type IN ('user', 'group', 'plugin')",
            name="ck_plugin_agent_sessions_scope_type",
        ),
        sa.CheckConstraint(
            "(scope_type = 'plugin' AND scope_id = '') OR "
            "(scope_type IN ('user', 'group') AND scope_id <> '')",
            name="ck_plugin_agent_sessions_scope_id",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'closed', 'expired', 'blocked')",
            name="ck_plugin_agent_sessions_status",
        ),
        sa.CheckConstraint(
            "persistence IN ('ephemeral', 'durable')",
            name="ck_plugin_agent_sessions_persistence",
        ),
        sa.CheckConstraint(
            "context_profile IN ('none', 'current_user', 'current_group')",
            name="ck_plugin_agent_sessions_context_profile",
        ),
        sa.CheckConstraint(
            "length(instructions) >= 1 AND length(instructions) <= 8000",
            name="ck_plugin_agent_sessions_instructions",
        ),
        sa.CheckConstraint("next_sequence >= 1", name="ck_plugin_agent_sessions_sequence"),
        sa.CheckConstraint("turn_count >= 0", name="ck_plugin_agent_sessions_turn_count"),
        sa.ForeignKeyConstraint(
            ["plugin_id"], ["plugin_installations.plugin_id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id"),
    )
    op.create_index(
        "ix_plugin_agent_sessions_plugin_scope",
        "plugin_agent_sessions",
        ["plugin_id", "scope_type", "scope_id"],
    )
    op.create_index(
        "ix_plugin_agent_sessions_owner_active",
        "plugin_agent_sessions",
        ["owner_user_id", "status", "last_active_at"],
    )
    op.create_index("ix_plugin_agent_sessions_expires", "plugin_agent_sessions", ["expires_at"])

    op.create_table(
        "plugin_agent_messages",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("sender_user_id", sa.String(length=64), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), server_default="{}", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool')",
            name="ck_plugin_agent_messages_role",
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_plugin_agent_messages_sequence"),
        sa.ForeignKeyConstraint(["sender_user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["session_id"], ["plugin_agent_sessions.session_id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "session_id", "sequence", name="uq_plugin_agent_messages_session_sequence"
        ),
    )
    op.create_index(
        "ix_plugin_agent_messages_session_created",
        "plugin_agent_messages",
        ["session_id", "created_at"],
    )
    op.create_index("ix_plugin_agent_messages_sender", "plugin_agent_messages", ["sender_user_id"])


def downgrade() -> None:
    """Remove only Planner and Plugin API v1 persistence."""

    op.drop_index("ix_plugin_agent_messages_sender", table_name="plugin_agent_messages")
    op.drop_index("ix_plugin_agent_messages_session_created", table_name="plugin_agent_messages")
    op.drop_table("plugin_agent_messages")
    op.drop_index("ix_plugin_agent_sessions_expires", table_name="plugin_agent_sessions")
    op.drop_index("ix_plugin_agent_sessions_owner_active", table_name="plugin_agent_sessions")
    op.drop_index("ix_plugin_agent_sessions_plugin_scope", table_name="plugin_agent_sessions")
    op.drop_table("plugin_agent_sessions")
    op.drop_index("ix_planner_runs_finished", table_name="planner_runs")
    op.drop_index("ix_planner_runs_conversation_created", table_name="planner_runs")
    op.drop_index("ix_planner_runs_created", table_name="planner_runs")
    op.drop_table("planner_runs")
    op.drop_index("ix_plugin_audit_events_actor_created", table_name="plugin_audit_events")
    op.drop_index("ix_plugin_audit_events_plugin_created", table_name="plugin_audit_events")
    op.drop_table("plugin_audit_events")
    op.drop_index("ix_plugin_state_subject", table_name="plugin_state")
    op.drop_index("ix_plugin_state_expires", table_name="plugin_state")
    op.drop_index("ix_plugin_state_plugin_namespace", table_name="plugin_state")
    op.drop_table("plugin_state")
    op.drop_index("ix_plugin_config_values_plugin_scope", table_name="plugin_config_values")
    op.drop_table("plugin_config_values")
    op.drop_index("ix_plugin_installations_updated", table_name="plugin_installations")
    op.drop_index("ix_plugin_installations_status_enabled", table_name="plugin_installations")
    op.drop_table("plugin_installations")
