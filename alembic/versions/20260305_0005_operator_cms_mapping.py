"""add operator cms mapping fields

Revision ID: 20260305_0005
Revises: 20260304_0004
Create Date: 2026-03-05 12:30:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260305_0005"
down_revision: str | None = "20260304_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("operator", sa.Column("meta_user_id", sa.String(length=128), nullable=True))
    op.add_column("operator", sa.Column("cms_campaign_id", sa.Integer(), nullable=True))
    op.add_column("operator", sa.Column("cms_playlist_id", sa.Integer(), nullable=True))
    op.create_index("ix_operator_meta_user_id", "operator", ["meta_user_id"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_operator_meta_user_id", table_name="operator")
    op.drop_column("operator", "cms_playlist_id")
    op.drop_column("operator", "cms_campaign_id")
    op.drop_column("operator", "meta_user_id")
