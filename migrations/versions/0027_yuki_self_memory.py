"""Add single-instance Yuki self memory and conversation visibility.

Revision ID: 0027
Revises: 0026
Create Date: 2026-08-03
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0027"
down_revision: str | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE_CHECK = "scope_type IN ('person', 'person_group', 'group', 'self')"
_OLD_SCOPE_CHECK = "scope_type IN ('person', 'person_group', 'group')"
_AUTHORITY_CHECK = (
    "authority IN ('explicit', 'self_report', 'group_report', 'third_party', 'agent_reflection')"
)
_OLD_AUTHORITY_CHECK = "authority IN ('explicit', 'self_report', 'group_report', 'third_party')"
_IDENTITY_CHECK = (
    "(scope_type = 'person' AND subject_user_id IS NOT NULL AND group_id IS NULL) OR "
    "(scope_type = 'person_group' AND subject_user_id IS NOT NULL AND group_id IS NOT NULL) OR "
    "(scope_type = 'group' AND subject_user_id IS NULL AND group_id IS NOT NULL) OR "
    "(scope_type = 'self' AND subject_user_id IS NULL AND group_id IS NULL)"
)
_OLD_IDENTITY_CHECK = (
    "(scope_type = 'person' AND subject_user_id IS NOT NULL AND group_id IS NULL) OR "
    "(scope_type = 'person_group' AND subject_user_id IS NOT NULL AND group_id IS NOT NULL) OR "
    "(scope_type = 'group' AND subject_user_id IS NULL AND group_id IS NOT NULL)"
)
_VISIBILITY_CHECK = (
    "(scope_type != 'self' AND visibility_type IS NULL AND visibility_user_id IS NULL AND "
    "visibility_group_id IS NULL) OR (scope_type = 'self' AND ("
    "(visibility_type = 'global' AND visibility_user_id IS NULL AND "
    "visibility_group_id IS NULL) OR "
    "(visibility_type = 'private' AND visibility_user_id IS NOT NULL AND "
    "visibility_group_id IS NULL) OR "
    "(visibility_type = 'group' AND visibility_user_id IS NULL AND "
    "visibility_group_id IS NOT NULL)))"
)
_EVIDENCE_RELATIONS = (
    "relation IN ('self_statement', 'group_statement', 'third_party_statement', "
    "'explicit_command', 'confirmation', 'correction', 'retraction', 'rebuild', "
    "'agent_reflection')"
)
_OLD_EVIDENCE_RELATIONS = (
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
    """Add only deterministic schema state; no self memories are synthesized."""

    _drop_fts_triggers()
    with op.batch_alter_table("memory_facts", recreate="always") as batch:
        batch.drop_constraint("ck_memory_facts_scope_type", type_="check")
        batch.drop_constraint("ck_memory_facts_authority", type_="check")
        batch.drop_constraint("ck_memory_facts_scope_identity", type_="check")
        batch.add_column(sa.Column("visibility_type", sa.String(length=16), nullable=True))
        batch.add_column(sa.Column("visibility_user_id", sa.String(length=64), nullable=True))
        batch.add_column(sa.Column("visibility_group_id", sa.String(length=64), nullable=True))
        batch.create_check_constraint("ck_memory_facts_scope_type", _SCOPE_CHECK)
        batch.create_check_constraint("ck_memory_facts_authority", _AUTHORITY_CHECK)
        batch.create_check_constraint(
            "ck_memory_facts_agent_reflection_scope",
            "authority != 'agent_reflection' OR scope_type = 'self'",
        )
        batch.create_check_constraint("ck_memory_facts_scope_identity", _IDENTITY_CHECK)
        batch.create_check_constraint(
            "ck_memory_facts_self_visibility",
            _VISIBILITY_CHECK,
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

    with op.batch_alter_table("memory_evidence", recreate="always") as batch:
        batch.drop_constraint("ck_memory_evidence_relation", type_="check")
        batch.drop_constraint("ck_memory_evidence_authority", type_="check")
        batch.create_check_constraint("ck_memory_evidence_relation", _EVIDENCE_RELATIONS)
        batch.create_check_constraint("ck_memory_evidence_authority", _AUTHORITY_CHECK)
    _create_fts_triggers()


def downgrade() -> None:
    """Refuse to erase self memories; callers must export/remove them explicitly."""

    connection = op.get_bind()
    self_count = int(
        connection.execute(
            sa.text("SELECT count(*) FROM memory_facts WHERE scope_type = 'self'")
        ).scalar_one()
    )
    if self_count:
        raise RuntimeError("cannot downgrade while Yuki self memories exist")

    op.execute(
        "UPDATE memory_evidence SET relation = 'correction' WHERE relation = 'agent_reflection'"
    )
    op.execute(
        "UPDATE memory_evidence SET authority = 'self_report' WHERE authority = 'agent_reflection'"
    )
    with op.batch_alter_table("memory_evidence", recreate="always") as batch:
        batch.drop_constraint("ck_memory_evidence_relation", type_="check")
        batch.drop_constraint("ck_memory_evidence_authority", type_="check")
        batch.create_check_constraint("ck_memory_evidence_relation", _OLD_EVIDENCE_RELATIONS)
        batch.create_check_constraint("ck_memory_evidence_authority", _OLD_AUTHORITY_CHECK)

    _drop_fts_triggers()
    op.drop_index("uq_memory_facts_active_self_key", table_name="memory_facts")
    with op.batch_alter_table("memory_facts", recreate="always") as batch:
        batch.drop_constraint("ck_memory_facts_self_visibility", type_="check")
        batch.drop_constraint("ck_memory_facts_agent_reflection_scope", type_="check")
        batch.drop_constraint("ck_memory_facts_scope_identity", type_="check")
        batch.drop_constraint("ck_memory_facts_authority", type_="check")
        batch.drop_constraint("ck_memory_facts_scope_type", type_="check")
        batch.drop_column("visibility_group_id")
        batch.drop_column("visibility_user_id")
        batch.drop_column("visibility_type")
        batch.create_check_constraint("ck_memory_facts_scope_type", _OLD_SCOPE_CHECK)
        batch.create_check_constraint("ck_memory_facts_authority", _OLD_AUTHORITY_CHECK)
        batch.create_check_constraint("ck_memory_facts_scope_identity", _OLD_IDENTITY_CHECK)
    _create_fts_triggers()
