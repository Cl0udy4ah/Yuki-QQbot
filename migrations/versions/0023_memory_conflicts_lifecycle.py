"""Add Memory V2 conflict, evidence, audit, and lifecycle state.

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023"
down_revision: str | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_AUTHORITY_CHECK = "authority IN ('explicit', 'self_report', 'group_report', 'third_party')"
_EVIDENCE_RELATIONS = (
    "relation IN ('self_statement', 'group_statement', 'third_party_statement', "
    "'explicit_command', 'confirmation', 'correction', 'retraction', 'rebuild')"
)


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
    """Apply deterministic schema changes without model or embedding calls."""

    _drop_fts_triggers()
    with op.batch_alter_table("memory_facts", recreate="always") as batch:
        batch.drop_constraint("ck_memory_facts_status", type_="check")
        batch.add_column(
            sa.Column(
                "authority", sa.String(length=16), server_default="self_report", nullable=False
            )
        )
        batch.add_column(
            sa.Column(
                "conflict_state", sa.String(length=16), server_default="clear", nullable=False
            )
        )
        batch.add_column(sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column("invalidated_reason", sa.String(length=40), nullable=True))
        batch.create_check_constraint(
            "ck_memory_facts_status",
            "status IN ('active', 'contested', 'superseded', 'invalidated')",
        )
        batch.create_check_constraint("ck_memory_facts_authority", _AUTHORITY_CHECK)
        batch.create_check_constraint(
            "ck_memory_facts_conflict_state",
            "conflict_state IN ('clear', 'contested')",
        )
        batch.create_check_constraint(
            "ck_memory_facts_contested_state",
            "status != 'contested' OR conflict_state = 'contested'",
        )
        batch.create_check_constraint(
            "ck_memory_facts_invalidation_reason",
            "(status = 'invalidated' AND invalidated_reason IS NOT NULL) OR "
            "(status != 'invalidated' AND invalidated_reason IS NULL)",
        )
    op.execute(
        """
        UPDATE memory_facts
        SET authority = CASE
            WHEN source_type = 'explicit' THEN 'explicit'
            WHEN scope_type = 'group' THEN 'group_report'
            ELSE 'self_report'
        END,
        conflict_state = 'clear',
        last_confirmed_at = updated_at,
        invalidated_reason = CASE
            WHEN status = 'invalidated' THEN 'administrator_invalidated'
            ELSE NULL
        END
        """
    )
    with op.batch_alter_table("memory_facts") as batch:
        batch.alter_column(
            "last_confirmed_at", existing_type=sa.DateTime(timezone=True), nullable=False
        )

    with op.batch_alter_table("memory_evidence", recreate="always") as batch:
        batch.drop_constraint("ck_memory_evidence_relation", type_="check")
        batch.add_column(sa.Column("confidence", sa.Float(), server_default="1.0", nullable=False))
        batch.add_column(
            sa.Column(
                "authority", sa.String(length=16), server_default="self_report", nullable=False
            )
        )
        batch.create_check_constraint("ck_memory_evidence_relation", _EVIDENCE_RELATIONS)
        batch.create_check_constraint("ck_memory_evidence_confidence", "confidence BETWEEN 0 AND 1")
        batch.create_check_constraint("ck_memory_evidence_authority", _AUTHORITY_CHECK)
    op.execute(
        """
        UPDATE memory_evidence
        SET authority = CASE
            WHEN relation IN ('explicit_command', 'correction') THEN 'explicit'
            ELSE (
                SELECT authority FROM memory_facts WHERE id = memory_evidence.fact_id
            )
        END,
        confidence = CASE
            WHEN relation IN ('explicit_command', 'correction') THEN 1.0
            WHEN relation = 'rebuild' THEN 0.75
            WHEN (
                SELECT authority FROM memory_facts WHERE id = memory_evidence.fact_id
            ) = 'group_report' THEN 0.7
            WHEN (
                SELECT authority FROM memory_facts WHERE id = memory_evidence.fact_id
            ) = 'third_party' THEN 0.55
            ELSE 0.9
        END
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_fact_relations (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            source_fact_id INTEGER NOT NULL REFERENCES memory_facts(id) ON DELETE CASCADE,
            target_fact_id INTEGER NOT NULL REFERENCES memory_facts(id) ON DELETE CASCADE,
            relation_type VARCHAR(16) NOT NULL
                CHECK (relation_type IN ('supports', 'contradicts', 'refines', 'equivalent')),
            confidence FLOAT NOT NULL CHECK (confidence BETWEEN 0 AND 1),
            source_event_id INTEGER REFERENCES chat_events(id) ON DELETE SET NULL,
            created_at DATETIME NOT NULL,
            CONSTRAINT ck_memory_fact_relations_distinct
                CHECK (source_fact_id != target_fact_id),
            CONSTRAINT uq_memory_fact_relations_pair_type
                UNIQUE (source_fact_id, target_fact_id, relation_type)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_fact_relations_source "
        "ON memory_fact_relations (source_fact_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_fact_relations_target "
        "ON memory_fact_relations (target_fact_id)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS memory_fact_state_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
            fact_id INTEGER NOT NULL REFERENCES memory_facts(id) ON DELETE CASCADE,
            action VARCHAR(24) NOT NULL CHECK (
                action IN ('created', 'confirmed', 'superseded', 'contested',
                    'conflict_cleared', 'invalidated', 'restored', 'merged',
                    'expired', 'stale_invalidated')
            ),
            from_status VARCHAR(16),
            to_status VARCHAR(16),
            from_conflict_state VARCHAR(16),
            to_conflict_state VARCHAR(16),
            reason_code VARCHAR(64) NOT NULL,
            source_event_id INTEGER REFERENCES chat_events(id) ON DELETE SET NULL,
            actor_user_id VARCHAR(64) REFERENCES people(user_id) ON DELETE SET NULL,
            created_at DATETIME NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_fact_state_events_fact_created "
        "ON memory_fact_state_events (fact_id, created_at)"
    )
    _create_fts_triggers()


def downgrade() -> None:
    connection = op.get_bind()
    contested = int(
        connection.execute(
            sa.text(
                "SELECT count(*) FROM memory_facts "
                "WHERE status = 'contested' OR conflict_state = 'contested'"
            )
        ).scalar_one()
    )
    if contested:
        raise RuntimeError("cannot downgrade Memory V2 while contested facts exist")

    op.drop_index("ix_memory_fact_state_events_fact_created", table_name="memory_fact_state_events")
    op.drop_table("memory_fact_state_events")
    op.drop_index("ix_memory_fact_relations_target", table_name="memory_fact_relations")
    op.drop_index("ix_memory_fact_relations_source", table_name="memory_fact_relations")
    op.drop_table("memory_fact_relations")

    op.execute(
        """
        UPDATE memory_evidence
        SET relation = CASE
            WHEN relation IN ('group_statement', 'third_party_statement', 'confirmation')
                THEN 'self_statement'
            WHEN relation = 'retraction' THEN 'correction'
            ELSE relation
        END
        """
    )
    with op.batch_alter_table("memory_evidence", recreate="always") as batch:
        batch.drop_constraint("ck_memory_evidence_authority", type_="check")
        batch.drop_constraint("ck_memory_evidence_confidence", type_="check")
        batch.drop_constraint("ck_memory_evidence_relation", type_="check")
        batch.drop_column("authority")
        batch.drop_column("confidence")
        batch.create_check_constraint(
            "ck_memory_evidence_relation",
            "relation IN ('self_statement', 'explicit_command', 'correction', 'rebuild')",
        )

    _drop_fts_triggers()
    with op.batch_alter_table("memory_facts", recreate="always") as batch:
        batch.drop_constraint("ck_memory_facts_invalidation_reason", type_="check")
        batch.drop_constraint("ck_memory_facts_contested_state", type_="check")
        batch.drop_constraint("ck_memory_facts_conflict_state", type_="check")
        batch.drop_constraint("ck_memory_facts_authority", type_="check")
        batch.drop_constraint("ck_memory_facts_status", type_="check")
        batch.drop_column("invalidated_reason")
        batch.drop_column("last_confirmed_at")
        batch.drop_column("conflict_state")
        batch.drop_column("authority")
        batch.create_check_constraint(
            "ck_memory_facts_status",
            "status IN ('active', 'superseded', 'invalidated')",
        )
    _create_fts_triggers()
