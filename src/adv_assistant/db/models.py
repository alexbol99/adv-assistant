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
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from adv_assistant.db.base import Base, TimestampMixin, utcnow
from adv_assistant.db.enums import AdDraftStatus, Language


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
    generation_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    preview_reference_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    rendered_image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[AdDraftStatus] = mapped_column(
        Enum(AdDraftStatus, name="ad_draft_status", native_enum=False),
        nullable=False,
        default=AdDraftStatus.DRAFT,
    )

    operator: Mapped[Operator] = relationship(back_populates="drafts")
    published_ads: Mapped[list["PublishedAd"]] = relationship(
        back_populates="ad_draft",
        passive_deletes=True,
    )


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
