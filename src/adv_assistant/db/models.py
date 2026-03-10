import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adv_assistant.db.base import Base, TimestampMixin, utcnow
from adv_assistant.db.enums import (
    AdDraftStatus,
    AdRequestType,
    AdVariantRoundStatus,
    AdVariantStatus,
    ClassificationStatus,
    DraftProductStatus,
    Language,
    PendingQuestionType,
)


class BusinessProfile(TimestampMixin, Base):
    __tablename__ = "business_profile"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    business_scope: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        default="default",
        index=True,
        unique=True,
    )
    business_name: Mapped[str] = mapped_column(String(160), nullable=False)
    logo_url: Mapped[str] = mapped_column(Text, nullable=False)
    brand_colors: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    screen_type: Mapped[str] = mapped_column(String(32), nullable=False, default="tv")
    screen_aspect_ratio: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="16:9",
    )
    screen_orientation: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="landscape",
    )
    screen_placement: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="indoor",
    )
    screen_location: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        default="unspecified",
    )


class Operator(TimestampMixin, Base):
    __tablename__ = "operator"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    phone: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    language: Mapped[str] = mapped_column(String(2), nullable=False, default=Language.HE.value)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="ILS")
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)

    drafts: Mapped[list["AdDraft"]] = relationship(
        back_populates="operator",
        passive_deletes=True,
    )
    sessions: Mapped[list["ConversationSession"]] = relationship(
        back_populates="operator",
        passive_deletes=True,
    )


class AdDraft(TimestampMixin, Base):
    __tablename__ = "ad_draft"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operator_phone: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("operator.phone", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    product_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="ILS")
    promo_text: Mapped[str | None] = mapped_column(String(240), nullable=True)
    ean: Mapped[str | None] = mapped_column(String(32), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    enriched_brand: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enriched_category: Mapped[str | None] = mapped_column(String(120), nullable=True)
    enriched_description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    enriched_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    enrichment_source: Mapped[str] = mapped_column(String(32), nullable=False, default="none")
    enrichment_unavailable_notified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    generation_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    preview_reference_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[AdDraftStatus] = mapped_column(
        Enum(AdDraftStatus, name="ad_draft_status", native_enum=False),
        nullable=False,
        default=AdDraftStatus.DRAFT,
    )
    request_type: Mapped[AdRequestType] = mapped_column(
        Enum(AdRequestType, name="ad_request_type", native_enum=False),
        nullable=False,
        default=AdRequestType.UNSET,
    )
    classification_status: Mapped[ClassificationStatus] = mapped_column(
        Enum(ClassificationStatus, name="classification_status", native_enum=False),
        nullable=False,
        default=ClassificationStatus.PENDING,
    )
    is_classification_resolved: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    awaiting_product_confirmation: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )
    generation_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    selected_round_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    selected_variant_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)

    operator: Mapped[Operator] = relationship(back_populates="drafts")
    published_ads: Mapped[list["PublishedAd"]] = relationship(
        back_populates="ad_draft",
        passive_deletes=True,
    )
    identified_products: Mapped[list["DraftProduct"]] = relationship(
        back_populates="draft",
        passive_deletes=True,
    )
    variant_rounds: Mapped[list["AdVariantRound"]] = relationship(
        back_populates="draft",
        passive_deletes=True,
    )


class DraftProduct(TimestampMixin, Base):
    __tablename__ = "draft_product"
    __table_args__ = (
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_draft_product_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("ad_draft.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    status: Mapped[DraftProductStatus] = mapped_column(
        Enum(DraftProductStatus, name="draft_product_status", native_enum=False),
        nullable=False,
        default=DraftProductStatus.CANDIDATE,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)

    draft: Mapped[AdDraft] = relationship(back_populates="identified_products")


class AdVariantRound(TimestampMixin, Base):
    __tablename__ = "ad_variant_round"
    __table_args__ = (
        Index(
            "uq_ad_variant_round_active_per_draft",
            "draft_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("ad_draft.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[AdVariantRoundStatus] = mapped_column(
        Enum(AdVariantRoundStatus, name="ad_variant_round_status", native_enum=False),
        nullable=False,
        default=AdVariantRoundStatus.ACTIVE,
    )
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    draft: Mapped[AdDraft] = relationship(back_populates="variant_rounds")
    variants: Mapped[list["AdVariant"]] = relationship(
        back_populates="round",
        passive_deletes=True,
    )


class AdVariant(TimestampMixin, Base):
    __tablename__ = "ad_variant"
    __table_args__ = (
        UniqueConstraint("round_id", "slot_no", name="uq_ad_variant_round_slot"),
        CheckConstraint("slot_no IN (1, 2)", name="ck_ad_variant_slot_no"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    round_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("ad_variant_round.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    slot_no: Mapped[int] = mapped_column(Integer, nullable=False)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[AdVariantStatus] = mapped_column(
        Enum(AdVariantStatus, name="ad_variant_status", native_enum=False),
        nullable=False,
        default=AdVariantStatus.FAILED,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    round: Mapped[AdVariantRound] = relationship(back_populates="variants")


class ConversationSession(TimestampMixin, Base):
    __tablename__ = "conversation_session"
    __table_args__ = (
        UniqueConstraint("operator_phone", name="uq_conversation_session_operator_phone"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operator_phone: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("operator.phone", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    language: Mapped[str] = mapped_column(String(2), nullable=False, default=Language.HE.value)
    history: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    pending_question_type: Mapped[PendingQuestionType] = mapped_column(
        Enum(PendingQuestionType, name="pending_question_type", native_enum=False),
        nullable=False,
        default=PendingQuestionType.NONE,
    )
    pending_question_context: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    last_user_intent_hint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    current_draft_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid,
        ForeignKey("ad_draft.id", ondelete="SET NULL"),
        nullable=True,
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    operator: Mapped[Operator] = relationship(back_populates="sessions")
    current_draft: Mapped[AdDraft | None] = relationship(
        foreign_keys=[current_draft_id], lazy="joined"
    )


class PublishedAd(TimestampMixin, Base):
    __tablename__ = "published_ad"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    cms_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    ad_draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("ad_draft.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow
    )

    ad_draft: Mapped[AdDraft] = relationship(back_populates="published_ads")


class SystemConfig(TimestampMixin, Base):
    __tablename__ = "system_config"
    __table_args__ = (CheckConstraint("id = 1", name="ck_system_config_singleton"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    cms_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_language: Mapped[str] = mapped_column(
        String(2), nullable=False, default=Language.HE.value
    )
    default_currency: Mapped[str] = mapped_column(String(3), nullable=False, default="ILS")
    default_region: Mapped[str] = mapped_column(String(16), nullable=False, default="IL")
    auth_secret_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    retention_processed_message_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=30
    )
    retention_media_days: Mapped[int] = mapped_column(Integer, nullable=False, default=90)
    retention_audit_months: Mapped[int] = mapped_column(Integer, nullable=False, default=13)


class AuditEvent(Base):
    __tablename__ = "audit_event"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    operator_phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSON, nullable=False, default=dict
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )


class ProcessedInboundMessage(Base):
    __tablename__ = "processed_inbound_message"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    wamid: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    operator_phone: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=utcnow,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: utcnow() + timedelta(days=30),
        index=True,
    )
