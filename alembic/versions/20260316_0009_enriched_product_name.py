"""add enriched_product_name to ad_draft

Revision ID: 20260316_0009
Revises: 4c0f00668da0
Create Date: 2026-03-16 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260316_0009"
down_revision: str | None = "4c0f00668da0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ad_draft",
        sa.Column("enriched_product_name", sa.String(length=256), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ad_draft", "enriched_product_name")
