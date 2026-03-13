from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.ad_generation import (
    GenerationDraftInput,
    GenerationMode,
    GenerationPollResult,
    GenerationSubmission,
    NanoBananaJobStatus,
)
from adv_assistant.db.base import Base
from adv_assistant.db.enums import DraftProductStatus, PendingQuestionType
from adv_assistant.db.models import AdDraft, ConversationSession, DraftProduct
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.llm_gateway import (
    ExtractedAdFields,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.media_ingest import IngestedOperatorPhoto, MediaIngestError
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.product_discovery import (
    ProductDiscoveryCandidate,
    ProductDiscoveryResult,
    ProductDiscoveryStatus,
)
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


class FakeGateway:
    uses_external_llm = True

    def __init__(self, *, product_name: str = "Cottage Cheese") -> None:
        self._product_name = product_name

    async def classify_intent(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> IntentClassification:
        return IntentClassification(intent=Intent.CREATE_AD)

    async def extract_ad_fields(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> ExtractedAdFields:
        return ExtractedAdFields(product_name=self._product_name)

    async def generate_reply(
        self,
        *,
        intent: Intent,
        message_text: str,
        language: str,
        extracted_fields: ExtractedAdFields | None,
    ) -> ReplyGeneration:
        return ReplyGeneration(reply_text="fallback")


class StaticDiscoveryService:
    enabled = True

    def __init__(self, *results: ProductDiscoveryResult) -> None:
        self._results = list(results)
        self.calls = 0

    async def discover(
        self,
        *,
        language: str,
        message_text: str | None,
        product_name: str | None,
        photo_url: str | None,
        ean: str | None,
    ) -> ProductDiscoveryResult:
        self.calls += 1
        if self._results:
            return self._results.pop(0)
        return ProductDiscoveryResult(status=ProductDiscoveryStatus.NO_MATCH)

    async def close(self) -> None:
        return None


class FakeGenerationService:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0
        self.last_draft: GenerationDraftInput | None = None

    async def submit_for_draft(
        self,
        *,
        draft: GenerationDraftInput,
        mode: GenerationMode,
        instruction_text: str,
        wamid: str,
        width: int,
        height: int,
    ) -> GenerationSubmission:
        self.calls += 1
        self.last_draft = draft
        return GenerationSubmission(
            job_id="job-1",
            idempotency_key="fixed",
            mode=mode,
            request_payload={},
        )

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
        return GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://example.com/preview.png",
        )

    async def close(self) -> None:
        return None


class StaticPhotoIngestor:
    def __init__(self, photo: IngestedOperatorPhoto) -> None:
        self._photo = photo

    async def ingest_whatsapp_image(self, *, media_id: str) -> IngestedOperatorPhoto:
        if not media_id:
            raise MediaIngestError("missing media id")
        return self._photo

    async def close(self) -> None:
        return None


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "product_discovery_t6.db"
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


async def test_high_confidence_single_match_requires_confirmation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500009001"
    await _seed_operator(session_factory, phone)
    discovery = StaticDiscoveryService(
        ProductDiscoveryResult(
            status=ProductDiscoveryStatus.SINGLE_MATCH,
            candidates=(
                ProductDiscoveryCandidate(
                    name="Tnuva Cottage Cheese 250g",
                    image_url="https://example.com/cottage.png",
                    source="catalog",
                    confidence=0.98,
                ),
            ),
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        product_discovery_service=discovery,
    )

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t6-single-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad for cottage cheese"}},
        )
    )

    assert first.reply_text is not None
    assert "match" in first.reply_text.lower() or "מצאתי" in first.reply_text

    async with session_scope(session_factory) as session:
        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        assert session_obj.pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION

        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.awaiting_product_confirmation is True
        assert draft.generation_ready is False

        candidates = (
            await session.execute(select(DraftProduct).where(DraftProduct.draft_id == draft.id))
        ).scalars()
        assert len(list(candidates)) == 1

    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t6-single-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "yes"}},
        )
    )

    assert second.reply_text is not None
    assert "saved" in second.reply_text.lower() or "שמרתי" in second.reply_text

    async with session_scope(session_factory) as session:
        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        assert session_obj.pending_question_type == PendingQuestionType.NONE

        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.awaiting_product_confirmation is False
        assert draft.product_name == "Tnuva Cottage Cheese 250g"
        assert draft.photo_url == "https://example.com/cottage.png"
        assert draft.generation_ready is True


async def test_multiple_candidates_require_selection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500009002"
    await _seed_operator(session_factory, phone)
    discovery = StaticDiscoveryService(
        ProductDiscoveryResult(
            status=ProductDiscoveryStatus.MULTIPLE_CANDIDATES,
            candidates=(
                ProductDiscoveryCandidate(name="Sprite 1.5L", source="catalog", confidence=0.88),
                ProductDiscoveryCandidate(name="7UP 1.5L", source="catalog", confidence=0.81),
            ),
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(product_name="Lemon Soda"),
        product_discovery_service=discovery,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t6-multi-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad for lemon soda"}},
        )
    )

    assert result.reply_text is not None
    assert "1." in result.reply_text
    assert "2." in result.reply_text

    select_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t6-multi-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "2"}},
        )
    )

    assert select_result.reply_text is not None

    async with session_scope(session_factory) as session:
        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        assert session_obj.pending_question_type == PendingQuestionType.NONE

        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.awaiting_product_confirmation is False
        assert draft.product_name == "7UP 1.5L"

        candidates = (
            await session.execute(select(DraftProduct).where(DraftProduct.draft_id == draft.id))
        ).scalars()
        status_by_name = {candidate.name: candidate.status for candidate in candidates}
        assert status_by_name["7UP 1.5L"] == DraftProductStatus.CONFIRMED
        assert status_by_name["Sprite 1.5L"] == DraftProductStatus.REJECTED


async def test_operator_photo_is_preserved_when_discovery_has_no_external_image(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500009003"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(product_name="Chocolate Milk"),
        product_discovery_service=StaticDiscoveryService(
            ProductDiscoveryResult(
                status=ProductDiscoveryStatus.SINGLE_MATCH,
                candidates=(
                    ProductDiscoveryCandidate(
                        name="Yotvata Chocolate Milk",
                        image_url=None,
                        source="catalog",
                        confidence=0.95,
                    ),
                ),
            )
        ),
        operator_photo_ingestor=StaticPhotoIngestor(
            IngestedOperatorPhoto(
                public_url="https://example.com/operator-photo.jpg",
                object_name="operator-photos/photo.jpg",
                content_type="image/jpeg",
                content=b"jpeg-bytes",
            )
        ),
    )

    await processor.process(
        InboundTaskPayload(
            wamid="wamid-t6-photo-1",
            operator_phone=phone,
            raw_message={"type": "image", "image": {"id": "media-1"}},
        )
    )
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-t6-photo-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad for chocolate milk"}},
        )
    )
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-t6-photo-3",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "yes"}},
        )
    )

    async with session_scope(session_factory) as session:
        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.photo_url == "https://example.com/operator-photo.jpg"
        assert draft.product_name == "Yotvata Chocolate Milk"
        assert draft.generation_ready is True


async def test_no_discovery_result_proceeds_with_operator_text_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500009004"
    await _seed_operator(session_factory, phone)
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(product_name="Orange Juice"),
        product_discovery_service=StaticDiscoveryService(
            ProductDiscoveryResult(status=ProductDiscoveryStatus.NO_MATCH)
        ),
        ad_generation_service=generation,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-t6-no-match-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad for orange juice"}},
        )
    )

    assert result.deterministic_action == "generation_completed"
    assert generation.calls == 1
    assert generation.last_draft is not None
    assert generation.last_draft.product_name == "Orange Juice"

    async with session_scope(session_factory) as session:
        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        assert session_obj.pending_question_type == PendingQuestionType.NONE

        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.awaiting_product_confirmation is False
        assert draft.generation_ready is True
