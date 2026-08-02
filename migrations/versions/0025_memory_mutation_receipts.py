"""Add auditable and idempotent Memory V2 mutation receipts.

Revision ID: 0025
Revises: 0024
Create Date: 2026-08-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025"
down_revision: str | None = "0024"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the receipt table without scanning or rewriting existing memories."""

    op.create_table(
        "memory_mutation_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mutation_id", sa.String(36), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("claim_fingerprint", sa.String(64), nullable=False),
        sa.Column("target_fingerprint", sa.String(64), nullable=False),
        sa.Column("trigger_event_id", sa.Integer(), nullable=False),
        sa.Column("conversation_key", sa.String(255), nullable=False),
        sa.Column("current_group_id", sa.String(64), nullable=True),
        sa.Column("turn_origin", sa.String(32), nullable=False),
        sa.Column("delegation_mode", sa.String(32), nullable=False),
        sa.Column("trigger_actor_user_id", sa.String(64), nullable=False),
        sa.Column("decision_actor_type", sa.String(16), nullable=False),
        sa.Column("decision_actor_id", sa.String(64), nullable=True),
        sa.Column("executed_by_bot_user_id", sa.String(64), nullable=True),
        sa.Column("requested_operation", sa.String(24), nullable=False),
        sa.Column("applied_operation", sa.String(24), nullable=False),
        sa.Column("old_fact_id", sa.Integer(), nullable=True),
        sa.Column("new_fact_id", sa.Integer(), nullable=True),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["trigger_event_id"], ["chat_events.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["current_group_id"], ["groups.group_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["trigger_actor_user_id"], ["people.user_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["executed_by_bot_user_id"], ["people.user_id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["old_fact_id"], ["memory_facts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["new_fact_id"], ["memory_facts.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("mutation_id", name="uq_memory_mutation_receipts_mutation_id"),
        sa.UniqueConstraint("idempotency_key", name="uq_memory_mutation_receipts_idempotency"),
        sa.UniqueConstraint("claim_fingerprint", name="uq_memory_mutation_receipts_claim"),
        sa.CheckConstraint(
            "decision_actor_type IN "
            "('agent','worker','command','admin','plugin','reflection','system')",
            name="ck_memory_mutation_decision_actor_type",
        ),
        sa.CheckConstraint(
            "requested_operation IN "
            "('create','correct','invalidate','restore','contest','merge','reassign',"
            "'update_metadata')",
            name="ck_memory_mutation_requested_operation",
        ),
        sa.CheckConstraint(
            "applied_operation IN "
            "('create','correct','invalidate','restore','contest','merge','reassign',"
            "'update_metadata','merge_evidence','noop')",
            name="ck_memory_mutation_applied_operation",
        ),
        sa.CheckConstraint(
            "outcome IN "
            "('processing','committed','committed_as_contested','deduplicated',"
            "'no_change','rejected')",
            name="ck_memory_mutation_outcome",
        ),
    )
    op.create_index(
        "ix_memory_mutation_receipts_event_created",
        "memory_mutation_receipts",
        ["trigger_event_id", "created_at"],
    )
    op.create_index(
        "ix_memory_mutation_receipts_target_created",
        "memory_mutation_receipts",
        ["target_fingerprint", "created_at"],
    )
    op.create_index(
        "ix_memory_mutation_receipts_old_fact",
        "memory_mutation_receipts",
        ["old_fact_id"],
    )
    op.create_index(
        "ix_memory_mutation_receipts_new_fact",
        "memory_mutation_receipts",
        ["new_fact_id"],
    )


def downgrade() -> None:
    """Drop receipts only; facts, evidence, and state history remain untouched."""

    op.drop_table("memory_mutation_receipts")
