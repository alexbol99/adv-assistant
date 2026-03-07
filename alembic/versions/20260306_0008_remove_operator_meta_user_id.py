"""remove operator meta user id

Revision ID: 20260306_0008
Revises: 20260305_0007
Create Date: 2026-03-06 14:25:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy import inspect

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260306_0008"
down_revision: str | None = "20260305_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    index_names = {index["name"] for index in inspector.get_indexes("operator")}
    column_names = {column["name"] for column in inspector.get_columns("operator")}

    if "ix_operator_meta_user_id" in index_names:
        op.drop_index("ix_operator_meta_user_id", table_name="operator")
    if "meta_user_id" in column_names:
        op.drop_column("operator", "meta_user_id")


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    index_names = {index["name"] for index in inspector.get_indexes("operator")}
    column_names = {column["name"] for column in inspector.get_columns("operator")}

    if "meta_user_id" not in column_names:
        op.add_column("operator", sa.Column("meta_user_id", sa.String(length=128), nullable=True))
    if "ix_operator_meta_user_id" not in index_names:
        op.create_index("ix_operator_meta_user_id", "operator", ["meta_user_id"], unique=True)
