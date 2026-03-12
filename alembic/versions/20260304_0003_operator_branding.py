"""operator branding columns

Revision ID: 20260304_0003
Revises: 20260301_0002
Create Date: 2026-03-04 12:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260304_0003"
down_revision: str | None = "20260301_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("operator", sa.Column("business_name", sa.String(length=200), nullable=True))
    op.add_column("operator", sa.Column("logo_url", sa.Text(), nullable=True))
    op.add_column("operator", sa.Column("brand_colors", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("operator", "brand_colors")
    op.drop_column("operator", "logo_url")
    op.drop_column("operator", "business_name")
