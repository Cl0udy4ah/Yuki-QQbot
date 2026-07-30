"""Add Tool Kernel MCP metadata, artifacts, and invocation telemetry.

Revision ID: 0019
Revises: 0018
Create Date: 2026-07-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(inspect(bind).get_table_names())
    if "mcp_server_states" not in existing:
        op.create_table(
            "mcp_server_states",
            sa.Column("server_id", sa.String(length=64), nullable=False),
            sa.Column("transport", sa.String(length=32), nullable=False),
            sa.Column("config_hash", sa.String(length=64), nullable=False),
            sa.Column("enabled", sa.Boolean(), nullable=False),
            sa.Column("lifecycle", sa.String(length=32), nullable=False),
            sa.Column("status", sa.String(length=32), nullable=False),
            sa.Column("protocol_version", sa.String(length=64), nullable=False),
            sa.Column("server_name", sa.String(length=255), nullable=False),
            sa.Column("server_version", sa.String(length=128), nullable=False),
            sa.Column("server_instructions", sa.Text(), nullable=False),
            sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_refreshed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error_category", sa.String(length=128), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("server_id"),
        )
    if "mcp_tool_cache" not in existing:
        op.create_table(
            "mcp_tool_cache",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("server_id", sa.String(length=64), nullable=False),
            sa.Column("remote_tool_name", sa.String(length=255), nullable=False),
            sa.Column("model_name", sa.String(length=128), nullable=False),
            sa.Column("description", sa.Text(), nullable=False),
            sa.Column("compact_description", sa.String(length=500), nullable=False),
            sa.Column("input_schema_json", sa.Text(), nullable=False),
            sa.Column("output_schema_json", sa.Text(), nullable=False),
            sa.Column("annotations_json", sa.Text(), nullable=False),
            sa.Column("metadata_hash", sa.String(length=64), nullable=False),
            sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("model_name"),
            sa.UniqueConstraint(
                "server_id", "remote_tool_name", name="uq_mcp_tool_cache_server_tool"
            ),
        )
        op.create_index("ix_mcp_tool_cache_server", "mcp_tool_cache", ["server_id"])
    if "tool_artifacts" not in existing:
        op.create_table(
            "tool_artifacts",
            sa.Column("handle_id", sa.String(length=64), nullable=False),
            sa.Column("provider_id", sa.String(length=128), nullable=False),
            sa.Column("tool_name", sa.String(length=255), nullable=False),
            sa.Column("relative_path", sa.String(length=512), nullable=False),
            sa.Column("media_type", sa.String(length=128), nullable=False),
            sa.Column("byte_size", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("handle_id"),
        )
        op.create_index("ix_tool_artifacts_expires", "tool_artifacts", ["expires_at"])
    if "tool_invocations" not in existing:
        op.create_table(
            "tool_invocations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("conversation_key_hash", sa.String(length=64), nullable=False),
            sa.Column("provider_id", sa.String(length=128), nullable=False),
            sa.Column("tool_name", sa.String(length=255), nullable=False),
            sa.Column("success", sa.Boolean(), nullable=False),
            sa.Column("latency_seconds", sa.Float(), nullable=False),
            sa.Column("result_size", sa.Integer(), nullable=False),
            sa.Column("artifact_created", sa.Boolean(), nullable=False),
            sa.Column("error_category", sa.String(length=128), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint("latency_seconds >= 0", name="ck_tool_invocations_latency"),
            sa.CheckConstraint("result_size >= 0", name="ck_tool_invocations_result_size"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_tool_invocations_provider_created",
            "tool_invocations",
            ["provider_id", "created_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_tool_invocations_provider_created", table_name="tool_invocations")
    op.drop_table("tool_invocations")
    op.drop_index("ix_tool_artifacts_expires", table_name="tool_artifacts")
    op.drop_table("tool_artifacts")
    op.drop_index("ix_mcp_tool_cache_server", table_name="mcp_tool_cache")
    op.drop_table("mcp_tool_cache")
    op.drop_table("mcp_server_states")
