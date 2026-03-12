"""add product brand and session pending upload type

Revision ID: 20260304_0004
Revises: 20260304_0003
Create Date: 2026-03-04 14:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260304_0004"
down_revision: str | None = "20260304_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ad_draft", sa.Column("product_brand", sa.String(length=120), nullable=True))
    op.add_column(
        "conversation_session",
        sa.Column("pending_upload_type", sa.String(length=16), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversation_session", "pending_upload_type")
    op.drop_column("ad_draft", "product_brand")
