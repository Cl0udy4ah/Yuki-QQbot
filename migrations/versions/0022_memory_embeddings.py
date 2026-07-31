"""Add rebuildable Memory V2 dense-vector indexes.

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-01
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create only local derived-index metadata; never call a remote provider."""

    op.create_table(
        "memory_embedding_profiles",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fingerprint", sa.String(length=64), nullable=False),
        sa.Column("provider_id", sa.String(length=64), nullable=False),
        sa.Column("model_id", sa.String(length=128), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("output_type", sa.String(length=16), nullable=False),
        sa.Column("document_template_version", sa.Integer(), nullable=False),
        sa.Column("endpoint_identity", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("dimensions > 0", name="ck_memory_embedding_profiles_dimensions"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fingerprint", name="uq_memory_embedding_profiles_fingerprint"),
    )
    op.create_table(
        "memory_embeddings",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("vector_blob", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["memory_embedding_profiles.id"], ondelete="CASCADE"
        ),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_memory_embeddings_content_hash_length"
        ),
        sa.CheckConstraint("length(vector_blob) > 0", name="ck_memory_embeddings_vector_nonempty"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_id", "profile_id", name="uq_memory_embeddings_fact_profile"),
    )
    op.create_index(
        "ix_memory_embeddings_profile_fact",
        "memory_embeddings",
        ["profile_id", "fact_id"],
    )
    op.create_table(
        "memory_embedding_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("fact_id", sa.Integer(), nullable=False),
        sa.Column("profile_id", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("error_category", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'done', 'failed')",
            name="ck_memory_embedding_jobs_status",
        ),
        sa.CheckConstraint("attempts >= 0", name="ck_memory_embedding_jobs_attempts"),
        sa.CheckConstraint(
            "length(content_hash) = 64", name="ck_memory_embedding_jobs_content_hash_length"
        ),
        sa.ForeignKeyConstraint(["fact_id"], ["memory_facts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["profile_id"], ["memory_embedding_profiles.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fact_id", "profile_id", name="uq_memory_embedding_jobs_fact_profile"),
    )
    op.create_index(
        "ix_memory_embedding_jobs_status_next",
        "memory_embedding_jobs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_memory_embedding_jobs_profile_status",
        "memory_embedding_jobs",
        ["profile_id", "status"],
    )


def downgrade() -> None:
    """Drop only the rebuildable embedding layer."""

    op.drop_index("ix_memory_embedding_jobs_profile_status", table_name="memory_embedding_jobs")
    op.drop_index("ix_memory_embedding_jobs_status_next", table_name="memory_embedding_jobs")
    op.drop_table("memory_embedding_jobs")
    op.drop_index("ix_memory_embeddings_profile_fact", table_name="memory_embeddings")
    op.drop_table("memory_embeddings")
    op.drop_table("memory_embedding_profiles")
