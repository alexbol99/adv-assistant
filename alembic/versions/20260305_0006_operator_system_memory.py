"""add operator system memory fields

Revision ID: 20260305_0006
Revises: 20260305_0005
Create Date: 2026-03-05 15:10:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260305_0006"
down_revision: str | None = "20260305_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("operator", sa.Column("store_type", sa.String(length=120), nullable=True))
    op.add_column("operator", sa.Column("creative_guidance", sa.String(length=500), nullable=True))


def downgrade() -> None:
    op.drop_column("operator", "creative_guidance")
    op.drop_column("operator", "store_type")
