"""Add trusted time settings and persistent automation runtime.

Revision ID: 0012
Revises: 0011
Create Date: 2026-07-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create automation tables and annotate scheduled ledger events."""

    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "person_time_settings" not in tables:
        op.create_table(
            "person_time_settings",
            sa.Column("user_id", sa.String(length=64), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["people.user_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("user_id"),
        )
    if "automations" not in tables:
        op.create_table(
            "automations",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("creator_user_id", sa.String(length=64), nullable=False),
            sa.Column("bot_user_id", sa.String(length=64), nullable=False),
            sa.Column("name", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("timezone", sa.String(length=64), nullable=False),
            sa.Column("schedule_json", sa.Text(), nullable=False),
            sa.Column("script_json", sa.Text(), nullable=False),
            sa.Column("script_hash", sa.String(length=64), nullable=False),
            sa.Column("required_capabilities_json", sa.Text(), nullable=False),
            sa.Column("authority_snapshot_json", sa.Text(), nullable=False),
            sa.Column("created_from_message_id", sa.String(length=128), nullable=False),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("run_count", sa.Integer(), server_default="0", nullable=False),
            sa.Column("max_runs", sa.Integer(), nullable=True),
            sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
            sa.Column("misfire_grace_seconds", sa.Integer(), server_default="1800", nullable=False),
            sa.Column("claimed_by", sa.String(length=64), nullable=True),
            sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('active', 'paused', 'completed', 'cancelled', 'failed', 'blocked')",
                name="ck_automations_status",
            ),
            sa.CheckConstraint("run_count >= 0", name="ck_automations_run_count"),
            sa.CheckConstraint(
                "consecutive_failures >= 0", name="ck_automations_consecutive_failures"
            ),
            sa.ForeignKeyConstraint(["creator_user_id"], ["people.user_id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_automations_status_next", "automations", ["status", "next_run_at"])
        op.create_index(
            "ix_automations_creator_updated", "automations", ["creator_user_id", "updated_at"]
        )
        op.create_index("ix_automations_claim", "automations", ["claimed_until", "claimed_by"])
    if "automation_versions" not in tables:
        op.create_table(
            "automation_versions",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("automation_id", sa.Integer(), nullable=False),
            sa.Column("version", sa.Integer(), nullable=False),
            sa.Column("script_json", sa.Text(), nullable=False),
            sa.Column("script_hash", sa.String(length=64), nullable=False),
            sa.Column("updated_by", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["automation_id"], ["automations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("automation_id", "version", name="uq_automation_versions_number"),
        )
    if "automation_runs" not in tables:
        op.create_table(
            "automation_runs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("automation_id", sa.Integer(), nullable=False),
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
            sa.Column("actual_started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("idempotency_key", sa.String(length=128), nullable=False),
            sa.Column("steps_completed", sa.Integer(), server_default="0", nullable=False),
            sa.Column("llm_calls", sa.Integer(), server_default="0", nullable=False),
            sa.Column("tool_calls", sa.Integer(), server_default="0", nullable=False),
            sa.Column("messages_sent", sa.Integer(), server_default="0", nullable=False),
            sa.Column("error_category", sa.String(length=64), nullable=True),
            sa.Column("result_summary_json", sa.Text(), server_default="{}", nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.CheckConstraint(
                "status IN ('running', 'succeeded', 'failed', 'skipped', 'missed', "
                "'uncertain', 'blocked')",
                name="ck_automation_runs_status",
            ),
            sa.ForeignKeyConstraint(["automation_id"], ["automations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("idempotency_key", name="uq_automation_runs_idempotency_key"),
            sa.UniqueConstraint(
                "automation_id", "scheduled_for", name="uq_automation_runs_scheduled_for"
            ),
        )
        op.create_index(
            "ix_automation_runs_automation_created",
            "automation_runs",
            ["automation_id", "created_at"],
        )
    if "automation_step_runs" not in tables:
        op.create_table(
            "automation_step_runs",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("run_id", sa.Integer(), nullable=False),
            sa.Column("step_id", sa.String(length=32), nullable=False),
            sa.Column("capability", sa.String(length=128), nullable=False),
            sa.Column("status", sa.String(length=16), nullable=False),
            sa.Column("input_summary_json", sa.Text(), server_default="{}", nullable=False),
            sa.Column("output_summary_json", sa.Text(), server_default="{}", nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error_category", sa.String(length=64), nullable=True),
            sa.ForeignKeyConstraint(["run_id"], ["automation_runs.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_automation_step_runs_run_step", "automation_step_runs", ["run_id", "step_id"]
        )
    columns = {column["name"] for column in inspect(bind).get_columns("chat_events")}
    if "origin" not in columns:
        op.add_column(
            "chat_events",
            sa.Column(
                "origin", sa.String(length=32), server_default="user_message", nullable=False
            ),
        )
    if "automation_id" not in columns:
        op.add_column("chat_events", sa.Column("automation_id", sa.Integer(), nullable=True))
    if "automation_run_id" not in columns:
        op.add_column("chat_events", sa.Column("automation_run_id", sa.Integer(), nullable=True))
    indexes = {index["name"] for index in inspect(bind).get_indexes("chat_events")}
    if "ix_chat_events_automation" not in indexes:
        op.create_index(
            "ix_chat_events_automation",
            "chat_events",
            ["automation_id", "automation_run_id"],
        )


def downgrade() -> None:
    """Remove only the 1.5 automation runtime and time preferences."""

    bind = op.get_bind()
    indexes = {index["name"] for index in inspect(bind).get_indexes("chat_events")}
    if "ix_chat_events_automation" in indexes:
        op.drop_index("ix_chat_events_automation", table_name="chat_events")
    columns = {column["name"] for column in inspect(bind).get_columns("chat_events")}
    for name in ("automation_run_id", "automation_id", "origin"):
        if name in columns:
            op.drop_column("chat_events", name)
    tables = set(inspect(bind).get_table_names())
    for name in (
        "automation_step_runs",
        "automation_runs",
        "automation_versions",
        "automations",
        "person_time_settings",
    ):
        if name in tables:
            op.drop_table(name)
