"""Add validated runtime configuration and administrator audit storage.

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create only the non-destructive runtime administrator tables."""

    op.create_table(
        "runtime_config_overrides",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("config_key", sa.String(length=128), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("scope_id", sa.String(length=64), server_default="", nullable=False),
        sa.Column("value_json", sa.Text(), nullable=False),
        sa.Column("value_type", sa.String(length=16), nullable=False),
        sa.Column("apply_mode", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=64), nullable=False),
        sa.CheckConstraint(
            "scope_type IN ('global', 'group', 'user')",
            name="ck_runtime_config_overrides_scope_type",
        ),
        sa.CheckConstraint(
            "(scope_type = 'global' AND scope_id = '') OR "
            "(scope_type IN ('group', 'user') AND scope_id <> '')",
            name="ck_runtime_config_overrides_scope_id",
        ),
        sa.CheckConstraint(
            "value_type IN ('string', 'integer', 'number', 'boolean', 'enum')",
            name="ck_runtime_config_overrides_value_type",
        ),
        sa.CheckConstraint(
            "apply_mode IN ('hot', 'future_only', 'restart_required')",
            name="ck_runtime_config_overrides_apply_mode",
        ),
        sa.CheckConstraint(
            "version >= 1",
            name="ck_runtime_config_overrides_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "config_key",
            "scope_type",
            "scope_id",
            name="uq_runtime_config_override_scope",
        ),
    )
    op.create_index(
        "ix_runtime_config_overrides_scope_key",
        "runtime_config_overrides",
        ["scope_type", "scope_id", "config_key"],
        unique=False,
    )

    op.create_table(
        "admin_operation_events",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("actor_user_id", sa.String(length=64), nullable=False),
        sa.Column("trigger_message_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("conversation_key", sa.String(length=255), server_default="", nullable=False),
        sa.Column("capability", sa.String(length=64), nullable=False),
        sa.Column("operation", sa.String(length=128), nullable=False),
        sa.Column("target_type", sa.String(length=64), nullable=False),
        sa.Column("target_id", sa.String(length=128), server_default="", nullable=False),
        sa.Column("before_json", sa.Text(), server_default="null", nullable=False),
        sa.Column("after_json", sa.Text(), server_default="null", nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "duration_seconds >= 0",
            name="ck_admin_operation_events_duration",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_admin_operation_events_actor_created",
        "admin_operation_events",
        ["actor_user_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_operation_events_target_created",
        "admin_operation_events",
        ["target_type", "target_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_admin_operation_events_capability_created",
        "admin_operation_events",
        ["capability", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Remove only the 1.3 runtime administrator subsystem."""

    op.drop_index(
        "ix_admin_operation_events_capability_created",
        table_name="admin_operation_events",
    )
    op.drop_index(
        "ix_admin_operation_events_target_created",
        table_name="admin_operation_events",
    )
    op.drop_index(
        "ix_admin_operation_events_actor_created",
        table_name="admin_operation_events",
    )
    op.drop_table("admin_operation_events")
    op.drop_index(
        "ix_runtime_config_overrides_scope_key",
        table_name="runtime_config_overrides",
    )
    op.drop_table("runtime_config_overrides")
