from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.base import Base
from adv_assistant.db.enums import AdRequestType, ClassificationStatus, PendingQuestionType
from adv_assistant.db.repositories import ConversationSessionRepository, OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.llm_gateway import (
    ExtractedAdFields,
    ExtractedBrandingFields,
    ExtractedProductQuery,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.media_ingest import IngestedOperatorPhoto
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.product_resolution_models import ProductResolutionResult
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


class RequestTypeGateway:
    uses_external_llm = True

    def __init__(
        self,
        *,
        intents: list[Intent],
        ad_fields: list[ExtractedAdFields],
    ) -> None:
        self._intents = list(intents)
        self._ad_fields = list(ad_fields)

    async def classify_intent(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> IntentClassification:
        if self._intents:
            return IntentClassification(intent=self._intents.pop(0))
        return IntentClassification(intent=Intent.UNKNOWN)

    async def extract_ad_fields(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> ExtractedAdFields:
        if self._ad_fields:
            return self._ad_fields.pop(0)
        return ExtractedAdFields()

    async def extract_branding_fields(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ExtractedBrandingFields:
        return ExtractedBrandingFields()

    async def extract_product_query(
        self, *, message_text: str, language: str
    ) -> ExtractedProductQuery:
        return ExtractedProductQuery()

    async def generate_reply(
        self,
        *,
        intent: Intent,
        message_text: str,
        language: str,
        extracted_fields: ExtractedAdFields | None,
    ) -> ReplyGeneration:
        return ReplyGeneration(reply_text="fallback")


class ProductResolutionProbeService:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def resolve(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ProductResolutionResult:
        self.calls.append(message_text)
        return ProductResolutionResult(
            status="needs_clarification",
            brand=None,
            product_query=message_text,
            raw_user_text=message_text,
            clarification_question="מה שם המוצר המדויק?",
        )

    async def close(self) -> None:
        return None


class StaticPhotoIngestor:
    async def ingest_whatsapp_image(self, *, media_id: str) -> IngestedOperatorPhoto:
        return IngestedOperatorPhoto(
            public_url="https://storage.googleapis.com/test-media/operator-photos/t5.jpg",
            object_name="operator-photos/t5.jpg",
            content_type="image/jpeg",
            content=b"jpeg-bytes",
        )

    async def ingest_external_image_url(self, *, image_url: str) -> IngestedOperatorPhoto:
        return IngestedOperatorPhoto(
            public_url="https://storage.googleapis.com/test-media/operator-photos/t5-rehost.jpg",
            object_name="operator-photos/t5-rehost.jpg",
            content_type="image/jpeg",
            content=b"jpeg-bytes",
        )

    async def close(self) -> None:
        return None


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "conversation_flow_v1_t5.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_operator(factory: async_sessionmaker[AsyncSession], phone: str) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(
            phone=phone,
            active=True,
            business_name="Test Biz",
            logo_url="https://example.com/logo.png",
        )


async def test_ambiguous_create_ad_enters_classification_loop(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001000"
    await _seed_operator(session_factory, phone)
    gateway = RequestTypeGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[ExtractedAdFields()],
    )
    processor = InboundTaskProcessor(session_factory, llm_gateway=gateway)

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t5-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad"}},
        )
    )

    assert result.reply_text is not None
    assert "מוצר אחד" in result.reply_text
    assert result.intent == Intent.CREATE_AD.value

    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(phone)
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.CLASSIFICATION
        assert session_obj.last_user_intent_hint == Intent.CREATE_AD.value

        draft = session_obj.current_draft
        assert draft is not None
        assert draft.request_type == AdRequestType.UNSET
        assert draft.classification_status == ClassificationStatus.PENDING
        assert draft.is_classification_resolved is False


async def test_classification_followup_resolves_single_product_and_clears_pending_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001001"
    await _seed_operator(session_factory, phone)
    gateway = RequestTypeGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[ExtractedAdFields(), ExtractedAdFields(product_name="Milk")],
    )
    processor = InboundTaskProcessor(session_factory, llm_gateway=gateway)

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t5-2a",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad"}},
        )
    )
    assert "מוצר אחד" in (first.reply_text or "")

    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t5-2b",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "single product milk"}},
        )
    )

    assert second.reply_text == "fallback"

    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(phone)
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.NONE

        draft = session_obj.current_draft
        assert draft is not None
        assert draft.request_type == AdRequestType.SINGLE_PRODUCT
        assert draft.classification_status == ClassificationStatus.RESOLVED
        assert draft.is_classification_resolved is True
        assert draft.product_name == "Milk"


async def test_image_reply_during_classification_enters_image_first_product_confirmation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001002"
    await _seed_operator(session_factory, phone)
    gateway = RequestTypeGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[ExtractedAdFields()],
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        operator_photo_ingestor=StaticPhotoIngestor(),
    )

    await processor.process(
        InboundTaskPayload(
            wamid="wamid-t5-3a",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad"}},
        )
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t5-3b",
            operator_phone=phone,
            raw_message={"type": "image", "image": {"id": "media-1"}},
        )
    )

    assert result.reply_text is not None
    assert "זה המוצר שהתכוונת אליו" in result.reply_text
    assert result.deterministic_action == "product_confirmation_requested"
    assert result.generated_image_url == (
        "https://storage.googleapis.com/test-media/operator-photos/t5.jpg"
    )
    assert result.action_buttons is not None

    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(phone)
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION
        draft = session_obj.current_draft
        assert draft is not None
        assert draft.photo_url == "https://storage.googleapis.com/test-media/operator-photos/t5.jpg"
        assert draft.request_type == AdRequestType.SINGLE_PRODUCT
        assert draft.classification_status == ClassificationStatus.RESOLVED
        assert draft.is_classification_resolved is True
        assert draft.awaiting_product_confirmation is True


async def test_classification_followup_preserves_original_product_request_for_resolution(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001003"
    await _seed_operator(session_factory, phone)
    gateway = RequestTypeGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[ExtractedAdFields(), ExtractedAdFields()],
    )
    resolution_probe = ProductResolutionProbeService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        product_resolution_service=resolution_probe,
    )

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t5-4a",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "תעשה לי פרסומת לנביעות"}},
        )
    )
    assert "מוצר אחד" in (first.reply_text or "")

    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t5-4b",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מוצר אחד"}},
        )
    )

    assert second.reply_text == "מה שם המוצר המדויק?"
    assert len(resolution_probe.calls) == 1
    assert "נביעות" in resolution_probe.calls[0]
    assert "מוצר אחד" in resolution_probe.calls[0]
