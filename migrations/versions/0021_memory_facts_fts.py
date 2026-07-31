"""Add the rebuildable Memory V2 lexical FTS index.

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIRTUAL TABLE memory_facts_fts USING fts5(
            content,
            memory_key,
            category,
            content='memory_facts',
            content_rowid='id',
            tokenize='trigram'
        )
        """
    )
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
    op.execute(
        """
        INSERT INTO memory_facts_fts(rowid, content, memory_key, category)
        SELECT id, content, memory_key, category FROM memory_facts
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS memory_facts_fts_ai")
    op.execute("DROP TRIGGER IF EXISTS memory_facts_fts_ad")
    op.execute("DROP TRIGGER IF EXISTS memory_facts_fts_au")
    op.execute("DROP TABLE IF EXISTS memory_facts_fts")
