"""Add memory quality review state and short-lived claim candidates.

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-08
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _drop_fts_triggers() -> None:
    op.execute("DROP TRIGGER IF EXISTS memory_facts_fts_ai")
    op.execute("DROP TRIGGER IF EXISTS memory_facts_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS memory_facts_fts_au")


def _create_fts_triggers() -> None:
    op.execute(
        """
        CREATE TRIGGER memory_facts_fts_ai AFTER INSERT ON memory_facts BEGIN
            INSERT INTO memory_facts_fts(rowid, content, memory_key, category)
            VALUES (new.id, new.content, new.memory_key, new.category);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER memory_facts_fts_ad AFTER DELETE ON memory_facts BEGIN
            INSERT INTO memory_facts_fts(
                memory_facts_fts, rowid, content, memory_key, category
            ) VALUES ('delete', old.id, old.content, old.memory_key, old.category);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER memory_facts_fts_au
        AFTER UPDATE OF content, memory_key, category ON memory_facts BEGIN
            INSERT INTO memory_facts_fts(
                memory_facts_fts, rowid, content, memory_key, category
            ) VALUES ('delete', old.id, old.content, old.memory_key, old.category);
            INSERT INTO memory_facts_fts(rowid, content, memory_key, category)
            VALUES (new.id, new.content, new.memory_key, new.category);
        END
        """
    )


def upgrade() -> None:
    _drop_fts_triggers()
    op.drop_index("uq_memory_facts_active_self_key", table_name="memory_facts")
    with op.batch_alter_table("memory_facts", recreate="always") as batch:
        batch.add_column(
            sa.Column(
                "validation_version",
                sa.String(length=32),
                nullable=False,
                server_default="legacy",
            )
        )
        batch.add_column(sa.Column("last_audited_at", sa.DateTime(timezone=True)))
        batch.add_column(
            sa.Column(
                "review_state",
                sa.String(length=24),
                nullable=False,
                server_default="legacy_unreviewed",
            )
        )
        batch.create_check_constraint(
            "ck_memory_facts_review_state",
            "review_state IN ('legacy_unreviewed', 'verified', 'quarantined')",
        )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_memory_facts_active_self_key
        ON memory_facts (
            memory_key,
            visibility_type,
            COALESCE(visibility_user_id, ''),
            COALESCE(visibility_group_id, '')
        )
        WHERE status = 'active' AND scope_type = 'self'
        """
    )
    _create_fts_triggers()

    op.create_table(
        "memory_tool_receipts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_key_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "trigger_event_id",
            sa.Integer(),
            sa.ForeignKey("chat_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "bot_user_id",
            sa.String(length=64),
            sa.ForeignKey("people.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider_id", sa.String(length=128), nullable=False),
        sa.Column("tool_name", sa.String(length=255), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("result_excerpt", sa.Text(), nullable=False),
        sa.Column("result_characters", sa.Integer(), nullable=False),
        sa.Column("error_category", sa.String(length=128)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("result_characters >= 0", name="ck_memory_tool_receipts_size"),
    )
    op.create_index(
        "ix_memory_tool_receipts_conversation_created",
        "memory_tool_receipts",
        ["conversation_key_hash", "created_at"],
    )
    op.create_index("ix_memory_tool_receipts_expires", "memory_tool_receipts", ["expires_at"])
    with op.batch_alter_table("memory_evidence", recreate="always") as batch:
        batch.alter_column("event_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("tool_receipt_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_memory_evidence_tool_receipt",
            "memory_tool_receipts",
            ["tool_receipt_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_memory_evidence_fact_tool_receipt", ["fact_id", "tool_receipt_id"]
        )
        batch.create_check_constraint(
            "ck_memory_evidence_source",
            "(event_id IS NOT NULL AND tool_receipt_id IS NULL) OR "
            "(event_id IS NULL AND tool_receipt_id IS NOT NULL)",
        )

    op.create_table(
        "memory_self_reflection_runtime",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("last_scanned_event_id", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "memory_self_reflection_states",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_key_hash", sa.String(length=64), nullable=False),
        sa.Column("bot_user_id", sa.String(length=64), nullable=False),
        sa.Column("scope_type", sa.String(length=16), nullable=False),
        sa.Column("group_id", sa.String(length=64)),
        sa.Column("private_peer_user_id", sa.String(length=64)),
        sa.Column("last_event_id", sa.Integer(), nullable=False),
        sa.Column("latest_event_id", sa.Integer(), nullable=False),
        sa.Column("pending_events", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_characters", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pending_since", sa.DateTime(timezone=True)),
        sa.Column("has_yuki_reply", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("has_tool_result", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("high_value_signal", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_key_hash", name="uq_memory_self_reflection_state_key"),
        sa.CheckConstraint(
            "scope_type IN ('private','group')", name="ck_self_reflection_state_scope"
        ),
        sa.CheckConstraint(
            "pending_events >= 0 AND pending_characters >= 0",
            name="ck_self_reflection_state_pending",
        ),
    )
    op.create_index(
        "ix_memory_self_reflection_state_pending",
        "memory_self_reflection_states",
        ["pending_since", "last_event_id"],
    )
    op.create_table(
        "memory_self_reflection_runs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("conversation_key_hash", sa.String(length=64), nullable=False),
        sa.Column("scheduled_slot", sa.String(length=32), nullable=False),
        sa.Column("trigger_reason", sa.String(length=32), nullable=False),
        sa.Column("first_event_id", sa.Integer(), nullable=False),
        sa.Column("last_event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("proposal_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("committed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_category", sa.String(length=64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "conversation_key_hash",
            "scheduled_slot",
            name="uq_self_reflection_run_slot",
        ),
        sa.CheckConstraint(
            "status IN ('processing','completed','failed')",
            name="ck_self_reflection_run_status",
        ),
    )
    op.create_index(
        "ix_memory_self_reflection_runs_slot",
        "memory_self_reflection_runs",
        ["scheduled_slot", "status"],
    )

    with op.batch_alter_table("memory_jobs", recreate="always") as batch:
        batch.drop_constraint("ck_memory_jobs_outcome", type_="check")
        batch.create_check_constraint(
            "ck_memory_jobs_outcome",
            "outcome IS NULL OR outcome IN ('claims_applied', 'candidates_staged', "
            "'no_claims', 'all_rejected', 'already_processed')",
        )

    op.create_table(
        "memory_claim_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("candidate_type", sa.String(length=16), nullable=False),
        sa.Column("target_scope", sa.String(length=16), nullable=False),
        sa.Column("subject_user_id", sa.String(length=64)),
        sa.Column("group_id", sa.String(length=64)),
        sa.Column("target_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("normalized_memory_key", sa.String(length=128), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("subject_basis", sa.String(length=32), nullable=False),
        sa.Column("retention", sa.String(length=32), nullable=False),
        sa.Column("source_style", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("evidence_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("fingerprint", name="uq_memory_claim_candidates_fingerprint"),
        sa.CheckConstraint(
            "candidate_type IN ('memory','self')",
            name="ck_memory_claim_candidates_type",
        ),
        sa.CheckConstraint(
            "status IN ('pending','accepted','rejected','expired')",
            name="ck_memory_claim_candidates_status",
        ),
        sa.CheckConstraint(
            "evidence_count >= 1",
            name="ck_memory_claim_candidates_evidence",
        ),
    )
    op.create_index(
        "ix_memory_claim_candidates_status_expiry",
        "memory_claim_candidates",
        ["status", "expires_at"],
    )
    op.create_index(
        "ix_memory_claim_candidates_target",
        "memory_claim_candidates",
        ["target_fingerprint", "status"],
    )
    op.create_table(
        "memory_claim_candidate_evidence",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("memory_claim_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.Integer(),
            sa.ForeignKey("chat_events.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "candidate_id",
            "event_id",
            name="uq_memory_claim_candidate_evidence",
        ),
    )
    op.create_index(
        "ix_memory_claim_candidate_evidence_event",
        "memory_claim_candidate_evidence",
        ["event_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_self_reflection_runs_slot", table_name="memory_self_reflection_runs")
    op.drop_table("memory_self_reflection_runs")
    op.drop_index(
        "ix_memory_self_reflection_state_pending",
        table_name="memory_self_reflection_states",
    )
    op.drop_table("memory_self_reflection_states")
    op.drop_table("memory_self_reflection_runtime")
    with op.batch_alter_table("memory_evidence", recreate="always") as batch:
        batch.drop_constraint("ck_memory_evidence_source", type_="check")
        batch.drop_constraint("uq_memory_evidence_fact_tool_receipt", type_="unique")
        batch.drop_constraint("fk_memory_evidence_tool_receipt", type_="foreignkey")
        batch.drop_column("tool_receipt_id")
        batch.alter_column("event_id", existing_type=sa.Integer(), nullable=False)
    op.drop_index("ix_memory_tool_receipts_expires", table_name="memory_tool_receipts")
    op.drop_index(
        "ix_memory_tool_receipts_conversation_created",
        table_name="memory_tool_receipts",
    )
    op.drop_table("memory_tool_receipts")
    op.drop_index(
        "ix_memory_claim_candidate_evidence_event",
        table_name="memory_claim_candidate_evidence",
    )
    op.drop_table("memory_claim_candidate_evidence")
    op.drop_index("ix_memory_claim_candidates_target", table_name="memory_claim_candidates")
    op.drop_index(
        "ix_memory_claim_candidates_status_expiry",
        table_name="memory_claim_candidates",
    )
    op.drop_table("memory_claim_candidates")
    with op.batch_alter_table("memory_jobs", recreate="always") as batch:
        batch.drop_constraint("ck_memory_jobs_outcome", type_="check")
        batch.create_check_constraint(
            "ck_memory_jobs_outcome",
            "outcome IS NULL OR outcome IN ('claims_applied', 'no_claims', "
            "'all_rejected', 'already_processed')",
        )
    _drop_fts_triggers()
    op.drop_index("uq_memory_facts_active_self_key", table_name="memory_facts")
    with op.batch_alter_table("memory_facts", recreate="always") as batch:
        batch.drop_constraint("ck_memory_facts_review_state", type_="check")
        batch.drop_column("review_state")
        batch.drop_column("last_audited_at")
        batch.drop_column("validation_version")
    op.execute(
        """
        CREATE UNIQUE INDEX uq_memory_facts_active_self_key
        ON memory_facts (
            memory_key,
            visibility_type,
            COALESCE(visibility_user_id, ''),
            COALESCE(visibility_group_id, '')
        )
        WHERE status = 'active' AND scope_type = 'self'
        """
    )
    _create_fts_triggers()
