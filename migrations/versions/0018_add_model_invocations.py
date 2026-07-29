"""Add content-free model invocation telemetry.

Revision ID: 0018
Revises: 0017
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "model_invocations",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("task", sa.String(length=64), nullable=False),
        sa.Column("profile_id", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=64), nullable=False),
        sa.Column("model", sa.String(length=256), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("total_tokens", sa.Integer(), nullable=True),
        sa.Column("cached_prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("latency_seconds", sa.Float(), nullable=False),
        sa.Column("error_category", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("latency_seconds >= 0", name="ck_model_invocations_latency"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_invocations_task_created",
        "model_invocations",
        ["task", "created_at"],
    )
    op.create_index(
        "ix_model_invocations_profile_created",
        "model_invocations",
        ["profile_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_model_invocations_profile_created", table_name="model_invocations")
    op.drop_index("ix_model_invocations_task_created", table_name="model_invocations")
    op.drop_table("model_invocations")
