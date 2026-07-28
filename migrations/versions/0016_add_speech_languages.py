"""Add explicit target languages to local speech profiles and generations.

Revision ID: 0016
Revises: 0015
Create Date: 2026-07-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("speech_voice_profiles") as batch:
        batch.add_column(
            sa.Column(
                "supported_languages_json",
                sa.Text(),
                server_default="[]",
                nullable=False,
            )
        )
    op.execute(
        "UPDATE speech_voice_profiles "
        "SET supported_languages_json = json_array(language) "
        "WHERE supported_languages_json = '[]'"
    )
    with op.batch_alter_table("speech_generations") as batch:
        batch.add_column(
            sa.Column("target_language", sa.String(length=16), server_default="zh", nullable=False)
        )


def downgrade() -> None:
    with op.batch_alter_table("speech_generations") as batch:
        batch.drop_column("target_language")
    with op.batch_alter_table("speech_voice_profiles") as batch:
        batch.drop_column("supported_languages_json")
