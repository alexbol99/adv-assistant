import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from adv_assistant.db.base import utcnow
from adv_assistant.db.enums import (
    AdVariantRoundStatus,
    DraftProductStatus,
    PendingQuestionType,
)
from adv_assistant.db.models import (
    AdDraft,
    AdVariant,
    AdVariantRound,
    AuditEvent,
    BusinessProfile,
    ConversationSession,
    DraftProduct,
    Operator,
    ProcessedInboundMessage,
    PublishedAd,
    SystemConfig,
)


class OperatorRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        phone: str,
        display_name: str | None = None,
        language: str = "he",
        currency: str = "ILS",
        active: bool = True,
    ) -> Operator:
        operator = Operator(
            phone=phone,
            display_name=display_name,
            language=language,
            currency=currency,
            active=active,
        )
        self.session.add(operator)
        await self.session.flush()
        return operator

    async def get_by_phone(self, phone: str) -> Operator | None:
        result = await self.session.execute(select(Operator).where(Operator.phone == phone))
        return result.scalar_one_or_none()

    async def list_active(self) -> list[Operator]:
        result = await self.session.execute(select(Operator).where(Operator.active.is_(True)))
        return list(result.scalars().all())

    async def set_active(self, phone: str, active: bool) -> bool:
        result = await self.session.execute(
            update(Operator)
            .where(Operator.phone == phone)
            .values(active=active, updated_at=utcnow())
        )
        await self.session.flush()
        return (result.rowcount or 0) > 0


class ConversationSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_operator_phone(self, operator_phone: str) -> ConversationSession | None:
        result = await self.session.execute(
            select(ConversationSession).where(ConversationSession.operator_phone == operator_phone)
        )
        return result.scalar_one_or_none()

    async def create_or_update(
        self,
        *,
        operator_phone: str,
        language: str | None = None,
        history: list[dict[str, Any]] | None = None,
        current_draft_id: uuid.UUID | None = None,
        last_active_at: datetime | None = None,
        expires_at: datetime | None = None,
    ) -> ConversationSession:
        session_obj = await self.get_by_operator_phone(operator_phone)
        if session_obj is None:
            session_obj = ConversationSession(
                operator_phone=operator_phone,
                language=language or "he",
                history=history or [],
                current_draft_id=current_draft_id,
                last_active_at=last_active_at or utcnow(),
                expires_at=expires_at,
            )
            self.session.add(session_obj)
        else:
            if language is not None:
                session_obj.language = language
            if history is not None:
                session_obj.history = history
            if current_draft_id is not None:
                session_obj.current_draft_id = current_draft_id
            if last_active_at is not None:
                session_obj.last_active_at = last_active_at
            if expires_at is not None:
                session_obj.expires_at = expires_at
            session_obj.updated_at = utcnow()

        await self.session.flush()
        return session_obj

    async def set_pending_question(
        self,
        *,
        operator_phone: str,
        pending_question_type: PendingQuestionType,
        pending_question_context: dict[str, Any] | None = None,
        last_active_at: datetime | None = None,
    ) -> ConversationSession | None:
        session_obj = await self.get_by_operator_phone(operator_phone)
        if session_obj is None:
            return None

        session_obj.pending_question_type = pending_question_type
        session_obj.pending_question_context = pending_question_context or {}
        if last_active_at is not None:
            session_obj.last_active_at = last_active_at
        session_obj.updated_at = utcnow()
        await self.session.flush()
        return session_obj

    async def clear_pending_question(
        self,
        *,
        operator_phone: str,
        last_active_at: datetime | None = None,
    ) -> ConversationSession | None:
        return await self.set_pending_question(
            operator_phone=operator_phone,
            pending_question_type=PendingQuestionType.NONE,
            pending_question_context={},
            last_active_at=last_active_at,
        )

    async def delete_by_operator_phone(self, operator_phone: str) -> int:
        result = await self.session.execute(
            delete(ConversationSession).where(ConversationSession.operator_phone == operator_phone)
        )
        await self.session.flush()
        return result.rowcount or 0


class BusinessProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_scope(self, business_scope: str = "default") -> BusinessProfile | None:
        result = await self.session.execute(
            select(BusinessProfile).where(BusinessProfile.business_scope == business_scope)
        )
        return result.scalar_one_or_none()

    async def upsert(self, *, business_scope: str = "default", **fields: Any) -> BusinessProfile:
        profile = await self.get_by_scope(business_scope)
        if profile is None:
            profile = BusinessProfile(business_scope=business_scope, **fields)
            self.session.add(profile)
        else:
            for key, value in fields.items():
                setattr(profile, key, value)
            profile.updated_at = utcnow()
        await self.session.flush()
        return profile


class AdDraftRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, operator_phone: str, **fields: Any) -> AdDraft:
        draft = AdDraft(operator_phone=operator_phone, **fields)
        self.session.add(draft)
        await self.session.flush()
        return draft

    async def get_by_id(self, draft_id: uuid.UUID) -> AdDraft | None:
        result = await self.session.execute(select(AdDraft).where(AdDraft.id == draft_id))
        return result.scalar_one_or_none()

    async def get_by_id_for_operator(
        self,
        draft_id: uuid.UUID,
        operator_phone: str,
    ) -> AdDraft | None:
        result = await self.session.execute(
            select(AdDraft).where(
                AdDraft.id == draft_id,
                AdDraft.operator_phone == operator_phone,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_operator_phone(self, operator_phone: str) -> list[AdDraft]:
        result = await self.session.execute(
            select(AdDraft)
            .where(AdDraft.operator_phone == operator_phone)
            .order_by(AdDraft.created_at.desc())
        )
        return list(result.scalars().all())

    async def update_with_version(
        self,
        *,
        draft_id: uuid.UUID,
        expected_version: int,
        **fields: Any,
    ) -> AdDraft | None:
        update_values = dict(fields)
        update_values["version"] = AdDraft.version + 1
        update_values["updated_at"] = utcnow()

        result = await self.session.execute(
            update(AdDraft)
            .where(AdDraft.id == draft_id, AdDraft.version == expected_version)
            .values(**update_values)
        )
        if (result.rowcount or 0) == 0:
            return None
        await self.session.flush()
        return await self.get_by_id(draft_id)

    async def update_for_operator_with_version(
        self,
        *,
        draft_id: uuid.UUID,
        operator_phone: str,
        expected_version: int,
        **fields: Any,
    ) -> AdDraft | None:
        update_values = dict(fields)
        update_values["version"] = AdDraft.version + 1
        update_values["updated_at"] = utcnow()

        result = await self.session.execute(
            update(AdDraft)
            .where(
                AdDraft.id == draft_id,
                AdDraft.operator_phone == operator_phone,
                AdDraft.version == expected_version,
            )
            .values(**update_values)
        )
        if (result.rowcount or 0) == 0:
            return None
        await self.session.flush()
        return await self.get_by_id(draft_id)


class DraftProductRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, draft_id: uuid.UUID, **fields: Any) -> DraftProduct:
        candidate = DraftProduct(draft_id=draft_id, **fields)
        self.session.add(candidate)
        await self.session.flush()
        return candidate

    async def list_by_draft_id(self, draft_id: uuid.UUID) -> list[DraftProduct]:
        result = await self.session.execute(
            select(DraftProduct)
            .where(DraftProduct.draft_id == draft_id)
            .order_by(DraftProduct.position.asc(), DraftProduct.created_at.asc())
        )
        return list(result.scalars().all())

    async def get_by_id(self, candidate_id: uuid.UUID) -> DraftProduct | None:
        result = await self.session.execute(
            select(DraftProduct).where(DraftProduct.id == candidate_id)
        )
        return result.scalar_one_or_none()

    async def replace_candidates(
        self,
        *,
        draft_id: uuid.UUID,
        candidates: list[dict[str, Any]],
    ) -> list[DraftProduct]:
        await self.session.execute(delete(DraftProduct).where(DraftProduct.draft_id == draft_id))

        for idx, candidate_fields in enumerate(candidates):
            create_fields = dict(candidate_fields)
            create_fields.setdefault("status", DraftProductStatus.CANDIDATE)
            create_fields.setdefault("position", idx)
            self.session.add(DraftProduct(draft_id=draft_id, **create_fields))

        await self.session.flush()
        return await self.list_by_draft_id(draft_id)

    async def set_status(
        self,
        *,
        candidate_id: uuid.UUID,
        draft_id: uuid.UUID,
        status: DraftProductStatus,
    ) -> bool:
        result = await self.session.execute(
            update(DraftProduct)
            .where(DraftProduct.id == candidate_id, DraftProduct.draft_id == draft_id)
            .values(status=status, updated_at=utcnow())
        )
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def confirm_candidate(self, *, draft_id: uuid.UUID, candidate_id: uuid.UUID) -> bool:
        confirmed_result = await self.session.execute(
            update(DraftProduct)
            .where(DraftProduct.id == candidate_id, DraftProduct.draft_id == draft_id)
            .values(status=DraftProductStatus.CONFIRMED, updated_at=utcnow())
        )
        if (confirmed_result.rowcount or 0) == 0:
            return False

        await self.session.execute(
            update(DraftProduct)
            .where(DraftProduct.draft_id == draft_id, DraftProduct.id != candidate_id)
            .values(status=DraftProductStatus.REJECTED, updated_at=utcnow())
        )
        await self.session.flush()
        return True


class AdVariantRoundRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, round_id: uuid.UUID) -> AdVariantRound | None:
        result = await self.session.execute(
            select(AdVariantRound).where(AdVariantRound.id == round_id)
        )
        return result.scalar_one_or_none()

    async def get_active_by_draft_id(self, draft_id: uuid.UUID) -> AdVariantRound | None:
        result = await self.session.execute(
            select(AdVariantRound).where(
                AdVariantRound.draft_id == draft_id,
                AdVariantRound.status == AdVariantRoundStatus.ACTIVE,
            )
        )
        return result.scalar_one_or_none()

    async def list_by_draft_id(self, draft_id: uuid.UUID) -> list[AdVariantRound]:
        result = await self.session.execute(
            select(AdVariantRound)
            .where(AdVariantRound.draft_id == draft_id)
            .order_by(AdVariantRound.attempt_no.desc(), AdVariantRound.created_at.desc())
        )
        return list(result.scalars().all())

    async def create(
        self,
        *,
        draft_id: uuid.UUID,
        attempt_no: int,
        status: AdVariantRoundStatus = AdVariantRoundStatus.ACTIVE,
        failure_reason: str | None = None,
        variants: list[dict[str, Any]] | None = None,
    ) -> AdVariantRound:
        round_obj = AdVariantRound(
            draft_id=draft_id,
            attempt_no=attempt_no,
            status=status,
            failure_reason=failure_reason,
        )
        self.session.add(round_obj)
        await self.session.flush()

        if variants:
            for idx, variant_fields in enumerate(variants, start=1):
                create_fields = dict(variant_fields)
                slot_no = int(create_fields.pop("slot_no", idx))
                self.session.add(AdVariant(round_id=round_obj.id, slot_no=slot_no, **create_fields))
            await self.session.flush()

        return round_obj

    async def replace_active_round(
        self,
        *,
        draft_id: uuid.UUID,
        attempt_no: int,
        variants: list[dict[str, Any]] | None = None,
    ) -> AdVariantRound:
        await self.session.execute(
            update(AdVariantRound)
            .where(
                AdVariantRound.draft_id == draft_id,
                AdVariantRound.status == AdVariantRoundStatus.ACTIVE,
            )
            .values(status=AdVariantRoundStatus.SUPERSEDED, updated_at=utcnow())
        )
        return await self.create(
            draft_id=draft_id,
            attempt_no=attempt_no,
            status=AdVariantRoundStatus.ACTIVE,
            variants=variants,
        )


class AdVariantRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, round_id: uuid.UUID, slot_no: int, **fields: Any) -> AdVariant:
        variant = AdVariant(round_id=round_id, slot_no=slot_no, **fields)
        self.session.add(variant)
        await self.session.flush()
        return variant

    async def list_by_round_id(self, round_id: uuid.UUID) -> list[AdVariant]:
        result = await self.session.execute(
            select(AdVariant)
            .where(AdVariant.round_id == round_id)
            .order_by(AdVariant.slot_no.asc())
        )
        return list(result.scalars().all())


class PublishedAdRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(self, *, cms_id: str, ad_draft_id: uuid.UUID) -> PublishedAd:
        published_ad = PublishedAd(cms_id=cms_id, ad_draft_id=ad_draft_id)
        self.session.add(published_ad)
        await self.session.flush()
        return published_ad

    async def get_by_cms_id(self, cms_id: str) -> PublishedAd | None:
        result = await self.session.execute(select(PublishedAd).where(PublishedAd.cms_id == cms_id))
        return result.scalar_one_or_none()


class SystemConfigRepository:
    SINGLETON_ID = 1

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self) -> SystemConfig | None:
        return await self.session.get(SystemConfig, self.SINGLETON_ID)

    async def upsert(self, **fields: Any) -> SystemConfig:
        config = await self.get()
        if config is None:
            config = SystemConfig(id=self.SINGLETON_ID, **fields)
            self.session.add(config)
        else:
            for key, value in fields.items():
                setattr(config, key, value)
            config.updated_at = utcnow()
        await self.session.flush()
        return config


class AuditEventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def log(
        self,
        *,
        actor: str,
        action: str,
        metadata: dict[str, Any] | None = None,
        operator_phone: str | None = None,
        timestamp: datetime | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            actor=actor,
            action=action,
            operator_phone=operator_phone,
            metadata_json=metadata or {},
            timestamp=timestamp or utcnow(),
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_recent(self, limit: int = 100) -> list[AuditEvent]:
        result = await self.session.execute(
            select(AuditEvent).order_by(AuditEvent.timestamp.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def has_action_since(
        self,
        *,
        action: str,
        operator_phone: str,
        since: datetime,
    ) -> bool:
        result = await self.session.execute(
            select(AuditEvent.id)
            .where(
                AuditEvent.action == action,
                AuditEvent.operator_phone == operator_phone,
                AuditEvent.timestamp >= since,
            )
            .limit(1)
        )
        return result.scalar_one_or_none() is not None


class ProcessedInboundMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def mark_processed(self, *, wamid: str, operator_phone: str | None = None) -> bool:
        message = ProcessedInboundMessage(wamid=wamid, operator_phone=operator_phone)
        try:
            async with self.session.begin_nested():
                self.session.add(message)
                await self.session.flush()
        except IntegrityError:
            return False
        return True

    async def exists(self, wamid: str) -> bool:
        result = await self.session.execute(
            select(ProcessedInboundMessage.id).where(ProcessedInboundMessage.wamid == wamid)
        )
        return result.scalar_one_or_none() is not None

    async def delete_by_wamid(self, wamid: str) -> bool:
        result = await self.session.execute(
            delete(ProcessedInboundMessage).where(ProcessedInboundMessage.wamid == wamid)
        )
        await self.session.flush()
        return (result.rowcount or 0) > 0

    async def purge_older_than(self, cutoff: datetime) -> int:
        result = await self.session.execute(
            delete(ProcessedInboundMessage).where(ProcessedInboundMessage.expires_at < cutoff)
        )
        await self.session.flush()
        return result.rowcount or 0
