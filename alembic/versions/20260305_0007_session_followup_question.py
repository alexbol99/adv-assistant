"""add session pending followup question

Revision ID: 20260305_0007
Revises: 20260305_0006
Create Date: 2026-03-05 16:05:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260305_0007"
down_revision: str | None = "20260305_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversation_session",
        sa.Column("pending_followup_question", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation_session", "pending_followup_question")
