"""phase 1 initial schema

Revision ID: 20260301_0001
Revises:
Create Date: 2026-03-01 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260301_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ad_draft_status_enum = sa.Enum(
    "DRAFT",
    "GENERATING",
    "PREVIEW_READY",
    "APPROVED",
    "PUBLISHED",
    name="ad_draft_status",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "operator",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("phone"),
    )
    op.create_index("ix_operator_active", "operator", ["active"], unique=False)
    op.create_index("ix_operator_phone", "operator", ["phone"], unique=True)

    op.create_table(
        "ad_draft",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operator_phone", sa.String(length=32), nullable=False),
        sa.Column("product_name", sa.String(length=120), nullable=True),
        sa.Column("price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("promo_text", sa.String(length=240), nullable=True),
        sa.Column("ean", sa.String(length=32), nullable=True),
        sa.Column("photo_url", sa.Text(), nullable=True),
        sa.Column("generation_job_id", sa.String(length=128), nullable=True),
        sa.Column("preview_reference_url", sa.Text(), nullable=True),
        sa.Column("rendered_image_url", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", ad_draft_status_enum, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["operator_phone"], ["operator.phone"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ad_draft_generation_job_id", "ad_draft", ["generation_job_id"], unique=False
    )
    op.create_index("ix_ad_draft_operator_phone", "ad_draft", ["operator_phone"], unique=False)

    op.create_table(
        "conversation_session",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("operator_phone", sa.String(length=32), nullable=False),
        sa.Column("language", sa.String(length=2), nullable=False),
        sa.Column("history", sa.JSON(), nullable=False),
        sa.Column("current_draft_id", sa.Uuid(), nullable=True),
        sa.Column("last_active_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["current_draft_id"], ["ad_draft.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["operator_phone"], ["operator.phone"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("operator_phone", name="uq_conversation_session_operator_phone"),
    )
    op.create_index(
        "ix_conversation_session_last_active_at",
        "conversation_session",
        ["last_active_at"],
        unique=False,
    )
    op.create_index(
        "ix_conversation_session_operator_phone",
        "conversation_session",
        ["operator_phone"],
        unique=False,
    )

    op.create_table(
        "published_ad",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("cms_id", sa.String(length=128), nullable=False),
        sa.Column("ad_draft_id", sa.Uuid(), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["ad_draft_id"], ["ad_draft.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("cms_id"),
    )
    op.create_index("ix_published_ad_ad_draft_id", "published_ad", ["ad_draft_id"], unique=False)
    op.create_index("ix_published_ad_cms_id", "published_ad", ["cms_id"], unique=True)

    op.create_table(
        "system_config",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cms_base_url", sa.Text(), nullable=True),
        sa.Column("default_language", sa.String(length=2), nullable=False),
        sa.Column("default_currency", sa.String(length=3), nullable=False),
        sa.Column("default_region", sa.String(length=16), nullable=False),
        sa.Column("auth_secret_ref", sa.String(length=255), nullable=True),
        sa.Column("retention_processed_message_days", sa.Integer(), nullable=False),
        sa.Column("retention_media_days", sa.Integer(), nullable=False),
        sa.Column("retention_audit_months", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_system_config_singleton"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "audit_event",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("operator_phone", sa.String(length=32), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_event_action", "audit_event", ["action"], unique=False)
    op.create_index(
        "ix_audit_event_operator_phone", "audit_event", ["operator_phone"], unique=False
    )
    op.create_index("ix_audit_event_timestamp", "audit_event", ["timestamp"], unique=False)

    op.create_table(
        "processed_inbound_message",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("wamid", sa.String(length=128), nullable=False),
        sa.Column("operator_phone", sa.String(length=32), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wamid"),
    )
    op.create_index(
        "ix_processed_inbound_message_expires_at",
        "processed_inbound_message",
        ["expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_processed_inbound_message_operator_phone",
        "processed_inbound_message",
        ["operator_phone"],
        unique=False,
    )
    op.create_index(
        "ix_processed_inbound_message_processed_at",
        "processed_inbound_message",
        ["processed_at"],
        unique=False,
    )
    op.create_index(
        "ix_processed_inbound_message_wamid",
        "processed_inbound_message",
        ["wamid"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_processed_inbound_message_wamid", table_name="processed_inbound_message")
    op.drop_index(
        "ix_processed_inbound_message_processed_at", table_name="processed_inbound_message"
    )
    op.drop_index(
        "ix_processed_inbound_message_operator_phone", table_name="processed_inbound_message"
    )
    op.drop_index("ix_processed_inbound_message_expires_at", table_name="processed_inbound_message")
    op.drop_table("processed_inbound_message")

    op.drop_index("ix_audit_event_timestamp", table_name="audit_event")
    op.drop_index("ix_audit_event_operator_phone", table_name="audit_event")
    op.drop_index("ix_audit_event_action", table_name="audit_event")
    op.drop_table("audit_event")

    op.drop_table("system_config")

    op.drop_index("ix_published_ad_cms_id", table_name="published_ad")
    op.drop_index("ix_published_ad_ad_draft_id", table_name="published_ad")
    op.drop_table("published_ad")

    op.drop_index("ix_conversation_session_operator_phone", table_name="conversation_session")
    op.drop_index("ix_conversation_session_last_active_at", table_name="conversation_session")
    op.drop_table("conversation_session")

    op.drop_index("ix_ad_draft_operator_phone", table_name="ad_draft")
    op.drop_index("ix_ad_draft_generation_job_id", table_name="ad_draft")
    op.drop_table("ad_draft")

    op.drop_index("ix_operator_phone", table_name="operator")
    op.drop_index("ix_operator_active", table_name="operator")
    op.drop_table("operator")
