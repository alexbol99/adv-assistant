"""merge 0008 and flow-v1 0003 heads

Revision ID: 4c0f00668da0
Revises: 20260306_0008, 20260310_0003
Create Date: 2026-03-12 22:05:18.179367
"""

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "4c0f00668da0"
down_revision: str | None = ("20260306_0008", "20260310_0003")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
