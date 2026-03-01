"""phase 5 enrichment columns

Revision ID: 20260301_0002
Revises: 20260301_0001
Create Date: 2026-03-01 19:20:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260301_0002"
down_revision: str | None = "20260301_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("ad_draft", sa.Column("enriched_brand", sa.String(length=120), nullable=True))
    op.add_column("ad_draft", sa.Column("enriched_category", sa.String(length=120), nullable=True))
    op.add_column(
        "ad_draft",
        sa.Column("enriched_description", sa.String(length=500), nullable=True),
    )
    op.add_column("ad_draft", sa.Column("enriched_image_url", sa.Text(), nullable=True))
    op.add_column(
        "ad_draft",
        sa.Column(
            "enrichment_source",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "ad_draft",
        sa.Column("enrichment_unavailable_notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ad_draft", "enrichment_unavailable_notified_at")
    op.drop_column("ad_draft", "enrichment_source")
    op.drop_column("ad_draft", "enriched_image_url")
    op.drop_column("ad_draft", "enriched_description")
    op.drop_column("ad_draft", "enriched_category")
    op.drop_column("ad_draft", "enriched_brand")
