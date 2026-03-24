"""add marketing_brief to ad_draft

Revision ID: 20260323_0010
Revises: 20260316_0009
Create Date: 2026-03-23 22:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260323_0010"
down_revision: str | None = "20260316_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ad_draft",
        sa.Column("marketing_brief", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ad_draft", "marketing_brief")
