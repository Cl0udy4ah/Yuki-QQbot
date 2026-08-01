"""Add controlled event-ledger memory rebuild staging.

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-01
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024"
down_revision: str | None = "0023"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACTIVE = "'planned','extracting','extraction_paused','review','committing','commit_paused'"


def upgrade() -> None:
    """Create staging only; no event scan or model call is performed."""

    op.create_table(
        "memory_rebuild_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("public_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("selection_json", sa.Text(), nullable=False),
        sa.Column("selection_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_max_event_id", sa.Integer(), nullable=False),
        sa.Column("snapshot_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("scan_checkpoint_occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("scan_checkpoint_event_id", sa.Integer(), nullable=True),
        sa.Column("commit_checkpoint_event_id", sa.Integer(), nullable=True),
        sa.Column("commit_checkpoint_claim_index", sa.Integer(), nullable=True),
        sa.Column("created_by_user_id", sa.String(64), nullable=True),
        sa.Column("extraction_fingerprint", sa.String(64), nullable=False),
        sa.Column("plan_statistics_json", sa.Text(), nullable=False),
        sa.Column("extraction_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consolidation_requests", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_milliseconds", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("commit_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["people.user_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("public_id", name="uq_memory_rebuild_runs_public_id"),
        sa.CheckConstraint("snapshot_max_event_id >= 0", name="ck_memory_rebuild_snapshot"),
        sa.CheckConstraint(
            "extraction_requests >= 0 AND consolidation_requests >= 0 "
            "AND input_tokens >= 0 AND output_tokens >= 0 AND latency_milliseconds >= 0",
            name="ck_memory_rebuild_usage_nonnegative",
        ),
        sa.CheckConstraint(
            "status IN ('planned','extracting','extraction_paused','review','committing',"
            "'commit_paused','completed','cancelled','failed')",
            name="ck_memory_rebuild_run_status",
        ),
    )
    op.create_index(
        "ix_memory_rebuild_runs_status_created",
        "memory_rebuild_runs",
        ["status", "created_at"],
    )
    op.create_index(
        "ix_memory_rebuild_runs_created_by",
        "memory_rebuild_runs",
        ["created_by_user_id"],
    )

    with op.batch_alter_table("memory_jobs", recreate="always") as batch:
        batch.add_column(
            sa.Column("processing_source", sa.String(16), server_default="live", nullable=False)
        )
        batch.add_column(sa.Column("rebuild_run_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("outcome", sa.String(32), nullable=True))
        batch.add_column(sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_foreign_key(
            "fk_memory_jobs_rebuild_run",
            "memory_rebuild_runs",
            ["rebuild_run_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_check_constraint(
            "ck_memory_jobs_processing_source",
            "processing_source IN ('live', 'rebuild')",
        )
        batch.create_check_constraint(
            "ck_memory_jobs_outcome",
            "outcome IS NULL OR outcome IN "
            "('claims_applied', 'no_claims', 'all_rejected', 'already_processed')",
        )
    op.execute("UPDATE memory_jobs SET processing_source='live'")

    op.create_table(
        "memory_rebuild_items",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("source_event_hash", sa.String(64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("claim_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["memory_rebuild_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["chat_events.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "event_id", name="uq_memory_rebuild_items_run_event"),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_rebuild_items_attempts"),
        sa.CheckConstraint("claim_count >= 0", name="ck_memory_rebuild_items_claim_count"),
        sa.CheckConstraint(
            "status IN ('pending','extracting','staged','no_claims','skipped','failed','committed')",
            name="ck_memory_rebuild_item_status",
        ),
    )
    op.create_index(
        "ix_memory_rebuild_items_run_status_event",
        "memory_rebuild_items",
        ["run_id", "status", "event_id"],
    )
    op.create_index("ix_memory_rebuild_items_event", "memory_rebuild_items", ["event_id"])

    op.create_table(
        "memory_rebuild_proposals",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.Integer(), nullable=False),
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("claim_index", sa.Integer(), nullable=False),
        sa.Column("claim_json", sa.Text(), nullable=False),
        sa.Column("claim_hash", sa.String(64), nullable=False),
        sa.Column("scope_type", sa.String(16), nullable=False),
        sa.Column("subject_user_id", sa.String(64), nullable=True),
        sa.Column("group_id", sa.String(64), nullable=True),
        sa.Column("operation", sa.String(16), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("authority", sa.String(16), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("review_status", sa.String(16), nullable=False),
        sa.Column("commit_status", sa.String(16), nullable=False),
        sa.Column("actual_fact_id", sa.Integer(), nullable=True),
        sa.Column("actual_action", sa.String(32), nullable=True),
        sa.Column("actual_reason_code", sa.String(64), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_category", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewed_by_user_id", sa.String(64), nullable=True),
        sa.Column("committed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["memory_rebuild_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["memory_rebuild_items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["event_id"], ["chat_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["subject_user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["group_id"], ["groups.group_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actual_fact_id"], ["memory_facts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["people.user_id"], ondelete="SET NULL"),
        sa.UniqueConstraint("item_id", "claim_index", name="uq_memory_rebuild_proposal_claim"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_memory_rebuild_confidence"),
        sa.CheckConstraint(
            "review_status IN ('pending','approved','rejected')",
            name="ck_memory_rebuild_review_status",
        ),
        sa.CheckConstraint(
            "commit_status IN ('pending','committed','skipped','failed')",
            name="ck_memory_rebuild_commit_status",
        ),
    )
    op.create_index(
        "ix_memory_rebuild_proposals_run_review",
        "memory_rebuild_proposals",
        ["run_id", "review_status"],
    )
    op.create_index(
        "ix_memory_rebuild_proposals_run_commit",
        "memory_rebuild_proposals",
        ["run_id", "commit_status"],
    )
    op.create_index(
        "ix_memory_rebuild_proposals_subject",
        "memory_rebuild_proposals",
        ["subject_user_id"],
    )
    op.create_index(
        "ix_memory_rebuild_proposals_group",
        "memory_rebuild_proposals",
        ["group_id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    active = connection.execute(
        sa.text(f"SELECT COUNT(*) FROM memory_rebuild_runs WHERE status IN ({_ACTIVE})")
    ).scalar_one()
    if active:
        raise RuntimeError("cannot downgrade 0024 while memory rebuild runs are active")
    op.drop_table("memory_rebuild_proposals")
    op.drop_table("memory_rebuild_items")
    with op.batch_alter_table("memory_jobs", recreate="always") as batch:
        batch.drop_constraint("fk_memory_jobs_rebuild_run", type_="foreignkey")
        batch.drop_constraint("ck_memory_jobs_outcome", type_="check")
        batch.drop_constraint("ck_memory_jobs_processing_source", type_="check")
        batch.drop_column("completed_at")
        batch.drop_column("outcome")
        batch.drop_column("rebuild_run_id")
        batch.drop_column("processing_source")
    op.drop_table("memory_rebuild_runs")
