"""conversation flow v1 state foundations

Revision ID: 20260310_0003
Revises: 20260301_0002
Create Date: 2026-03-10 13:35:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260310_0003"
down_revision: str | None = "20260301_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ad_request_type_enum = sa.Enum(
    "unset",
    "single_product",
    "multi_product",
    "store_general",
    name="ad_request_type",
    native_enum=False,
)

classification_status_enum = sa.Enum(
    "pending",
    "resolved",
    name="classification_status",
    native_enum=False,
)

pending_question_type_enum = sa.Enum(
    "none",
    "onboarding",
    "classification",
    "product_confirmation",
    "candidate_selection",
    "missing_info",
    "generation_retry",
    name="pending_question_type",
    native_enum=False,
)

draft_product_status_enum = sa.Enum(
    "candidate",
    "confirmed",
    "rejected",
    name="draft_product_status",
    native_enum=False,
)

ad_variant_round_status_enum = sa.Enum(
    "active",
    "superseded",
    "failed",
    "published",
    name="ad_variant_round_status",
    native_enum=False,
)

ad_variant_status_enum = sa.Enum(
    "valid",
    "failed",
    name="ad_variant_status",
    native_enum=False,
)


def upgrade() -> None:
    op.create_table(
        "business_profile",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column(
            "business_scope",
            sa.String(length=64),
            nullable=False,
            server_default="default",
        ),
        sa.Column("business_name", sa.String(length=160), nullable=False),
        sa.Column("logo_url", sa.Text(), nullable=False),
        sa.Column(
            "brand_colors",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column(
            "screen_type",
            sa.String(length=32),
            nullable=False,
            server_default="tv",
        ),
        sa.Column(
            "screen_aspect_ratio",
            sa.String(length=16),
            nullable=False,
            server_default="16:9",
        ),
        sa.Column(
            "screen_orientation",
            sa.String(length=16),
            nullable=False,
            server_default="landscape",
        ),
        sa.Column(
            "screen_placement",
            sa.String(length=16),
            nullable=False,
            server_default="indoor",
        ),
        sa.Column(
            "screen_location",
            sa.String(length=255),
            nullable=False,
            server_default="unspecified",
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_business_profile_business_scope",
        "business_profile",
        ["business_scope"],
        unique=True,
    )

    op.add_column(
        "conversation_session",
        sa.Column(
            "pending_question_type",
            pending_question_type_enum,
            nullable=False,
            server_default="none",
        ),
    )
    op.add_column(
        "conversation_session",
        sa.Column(
            "pending_question_context",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
    op.add_column(
        "conversation_session",
        sa.Column("last_user_intent_hint", sa.String(length=64), nullable=True),
    )

    op.add_column(
        "ad_draft",
        sa.Column(
            "request_type",
            ad_request_type_enum,
            nullable=False,
            server_default="unset",
        ),
    )
    op.add_column(
        "ad_draft",
        sa.Column(
            "classification_status",
            classification_status_enum,
            nullable=False,
            server_default="pending",
        ),
    )
    op.add_column(
        "ad_draft",
        sa.Column(
            "is_classification_resolved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "ad_draft",
        sa.Column(
            "awaiting_product_confirmation",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "ad_draft",
        sa.Column(
            "generation_ready",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column("ad_draft", sa.Column("selected_round_id", sa.Uuid(), nullable=True))
    op.add_column("ad_draft", sa.Column("selected_variant_id", sa.Uuid(), nullable=True))
    op.create_index(
        "ix_ad_draft_selected_round_id",
        "ad_draft",
        ["selected_round_id"],
        unique=False,
    )
    op.create_index(
        "ix_ad_draft_selected_variant_id",
        "ad_draft",
        ["selected_variant_id"],
        unique=False,
    )

    op.create_table(
        "draft_product",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="unknown",
        ),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column(
            "status",
            draft_product_status_enum,
            nullable=False,
            server_default="candidate",
        ),
        sa.Column(
            "position",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("external_ref", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_draft_product_confidence_range",
        ),
        sa.ForeignKeyConstraint(["draft_id"], ["ad_draft.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_draft_product_draft_id", "draft_product", ["draft_id"], unique=False)
    op.create_index("ix_draft_product_status", "draft_product", ["status"], unique=False)

    op.create_table(
        "ad_variant_round",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("draft_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            ad_variant_round_status_enum,
            nullable=False,
            server_default="active",
        ),
        sa.Column(
            "attempt_no",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["draft_id"], ["ad_draft.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ad_variant_round_draft_id",
        "ad_variant_round",
        ["draft_id"],
        unique=False,
    )
    op.create_index(
        "uq_ad_variant_round_active_per_draft",
        "ad_variant_round",
        ["draft_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "ad_variant",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("round_id", sa.Uuid(), nullable=False),
        sa.Column("slot_no", sa.Integer(), nullable=False),
        sa.Column("image_url", sa.Text(), nullable=True),
        sa.Column("prompt_snapshot", sa.Text(), nullable=True),
        sa.Column(
            "status",
            ad_variant_status_enum,
            nullable=False,
            server_default="failed",
        ),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("slot_no IN (1, 2)", name="ck_ad_variant_slot_no"),
        sa.ForeignKeyConstraint(["round_id"], ["ad_variant_round.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("round_id", "slot_no", name="uq_ad_variant_round_slot"),
    )
    op.create_index("ix_ad_variant_round_id", "ad_variant", ["round_id"], unique=False)
    op.create_index("ix_ad_variant_status", "ad_variant", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_ad_variant_status", table_name="ad_variant")
    op.drop_index("ix_ad_variant_round_id", table_name="ad_variant")
    op.drop_table("ad_variant")

    op.drop_index("uq_ad_variant_round_active_per_draft", table_name="ad_variant_round")
    op.drop_index("ix_ad_variant_round_draft_id", table_name="ad_variant_round")
    op.drop_table("ad_variant_round")

    op.drop_index("ix_draft_product_status", table_name="draft_product")
    op.drop_index("ix_draft_product_draft_id", table_name="draft_product")
    op.drop_table("draft_product")

    op.drop_index("ix_ad_draft_selected_variant_id", table_name="ad_draft")
    op.drop_index("ix_ad_draft_selected_round_id", table_name="ad_draft")
    op.drop_column("ad_draft", "selected_variant_id")
    op.drop_column("ad_draft", "selected_round_id")
    op.drop_column("ad_draft", "generation_ready")
    op.drop_column("ad_draft", "awaiting_product_confirmation")
    op.drop_column("ad_draft", "is_classification_resolved")
    op.drop_column("ad_draft", "classification_status")
    op.drop_column("ad_draft", "request_type")

    op.drop_column("conversation_session", "last_user_intent_hint")
    op.drop_column("conversation_session", "pending_question_context")
    op.drop_column("conversation_session", "pending_question_type")

    op.drop_index("ix_business_profile_business_scope", table_name="business_profile")
    op.drop_table("business_profile")
