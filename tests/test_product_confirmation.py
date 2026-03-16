"""Tests for product confirmation step between discovery and generation."""

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.ad_generation import (
    GenerationDraftInput,
    GenerationMode,
    GenerationPollResult,
    GenerationSubmission,
    NanoBananaJobStatus,
)
from adv_assistant.db.base import Base
from adv_assistant.db.enums import AdDraftStatus, PendingQuestionType
from adv_assistant.db.repositories import ConversationSessionRepository, OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.llm_gateway import (
    BUTTON_CONFIRM_PRODUCT,
    BUTTON_REJECT_PRODUCT,
    ExtractedAdFields,
    ExtractedBrandingFields,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeGateway:
    uses_external_llm = True

    def __init__(self, *, product_name: str = "Cottage Cheese") -> None:
        self._product_name = product_name

    async def classify_intent(
        self, *, message_text: str, language: str, history: list[dict[str, str]]
    ) -> IntentClassification:
        return IntentClassification(intent=Intent.CREATE_AD)

    async def extract_ad_fields(
        self, *, message_text: str, language: str, history: list[dict[str, str]]
    ) -> ExtractedAdFields:
        return ExtractedAdFields(
            product_name=self._product_name,
            price=Decimal("19.90"),
            currency="ILS",
        )

    async def extract_branding_fields(
        self, *, message_text: str, language: str
    ) -> ExtractedBrandingFields:
        return ExtractedBrandingFields()

    async def generate_reply(
        self, *, intent: Intent, message_text: str, language: str,
        extracted_fields: ExtractedAdFields | None,
    ) -> ReplyGeneration:
        return ReplyGeneration(reply_text="fallback")


class FakeGenerationService:
    enabled = True

    def __init__(self) -> None:
        self.calls = 0
        self.last_draft: GenerationDraftInput | None = None

    async def submit_for_draft(
        self, *, draft: GenerationDraftInput, mode: GenerationMode,
        instruction_text: str, wamid: str, width: int, height: int,
    ) -> GenerationSubmission:
        self.calls += 1
        self.last_draft = draft
        return GenerationSubmission(
            job_id="job-123",
            idempotency_key="key-1",
            mode=mode,
            request_payload={},
        )

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
        return GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://img.example/ad.png",
        )

    async def close(self) -> None:
        pass


class TrackingWhatsAppClient:
    """Records outbound WhatsApp calls for test assertions."""

    def __init__(self) -> None:
        self.sent_texts: list[dict] = []
        self.sent_images: list[dict] = []
        self.sent_buttons: list[dict] = []

    async def send_text(self, *, to_phone: str, message: str) -> None:
        self.sent_texts.append({"to_phone": to_phone, "message": message})

    async def send_image(
        self, *, to_phone: str, image_url: str, caption: str | None = None
    ) -> None:
        self.sent_images.append(
            {"to_phone": to_phone, "image_url": image_url, "caption": caption}
        )

    async def send_buttons(
        self, *, to_phone: str, body_text: str, buttons: list[tuple[str, str]],
        header_image_url: str | None = None,
    ) -> None:
        self.sent_buttons.append({
            "to_phone": to_phone,
            "body_text": body_text,
            "buttons": buttons,
            "header_image_url": header_image_url,
        })

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "product_confirmation.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


PHONE = "+972500009000"


async def _seed_operator(
    factory: async_sessionmaker[AsyncSession],
    phone: str = PHONE,
) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(
            phone=phone,
            active=True,
            business_name="Test Biz",
            logo_url="https://example.com/logo.png",
        )


def _text_msg(body: str) -> dict:
    return {"type": "text", "text": {"body": body}}


def _button_msg(button_id: str) -> dict:
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "button_reply": {"id": button_id, "title": "..."},
        },
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_product_discovery_sends_confirmation_instead_of_generating(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """After product discovery, the system should send a confirmation message
    with two buttons and NOT start generation."""
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()
    wa_client = TrackingWhatsAppClient()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        ad_generation_service=gen_service,
        whatsapp_client=wa_client,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-1",
            operator_phone=PHONE,
            raw_message=_text_msg("מוצר אחד Cottage Cheese 19.90"),
        )
    )

    # Generation must NOT have been triggered.
    assert gen_service.calls == 0
    assert result.generated_image_url is None

    # The reply should be the confirmation prompt including the product name.
    assert result.reply_text is not None
    assert "מצאתי את המוצר הזה" in result.reply_text
    assert "Cottage Cheese" in result.reply_text

    # Confirmation buttons should have been sent via WhatsApp.
    assert len(wa_client.sent_buttons) == 1
    btn_call = wa_client.sent_buttons[0]
    button_ids = [b[0] for b in btn_call["buttons"]]
    assert BUTTON_CONFIRM_PRODUCT in button_ids
    assert BUTTON_REJECT_PRODUCT in button_ids
    # WhatsApp body text should also contain the product name.
    assert "Cottage Cheese" in btn_call["body_text"]

    # Session should be in PRODUCT_CONFIRMATION state.
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION

        draft = session_obj.current_draft
        assert draft is not None
        assert draft.awaiting_product_confirmation is True
        assert draft.status == AdDraftStatus.DRAFT


async def test_confirm_button_triggers_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the user clicks the confirm button, generation should proceed."""
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()
    wa_client = TrackingWhatsAppClient()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        ad_generation_service=gen_service,
        whatsapp_client=wa_client,
    )

    # Step 1: Send product message -> gets confirmation prompt.
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-2a",
            operator_phone=PHONE,
            raw_message=_text_msg("מוצר אחד Cottage Cheese 19.90"),
        )
    )
    assert gen_service.calls == 0

    # Step 2: Click confirm button.
    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-2b",
            operator_phone=PHONE,
            raw_message=_button_msg(BUTTON_CONFIRM_PRODUCT),
        )
    )

    # Generation should have been triggered.
    assert gen_service.calls == 1
    assert result.generated_image_url == "https://img.example/ad.png"

    # Session should be cleared from PRODUCT_CONFIRMATION state.
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        assert session_obj is not None
        assert session_obj.pending_question_type != PendingQuestionType.PRODUCT_CONFIRMATION

        draft = session_obj.current_draft
        assert draft is not None
        assert draft.awaiting_product_confirmation is False
        assert draft.status == AdDraftStatus.PREVIEW_READY


async def test_reject_button_blocks_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When the user clicks the reject button, generation must NOT happen."""
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()
    wa_client = TrackingWhatsAppClient()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        ad_generation_service=gen_service,
        whatsapp_client=wa_client,
    )

    # Step 1: Send product message -> gets confirmation prompt.
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-3a",
            operator_phone=PHONE,
            raw_message=_text_msg("מוצר אחד Cottage Cheese 19.90"),
        )
    )

    # Step 2: Click reject button.
    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-3b",
            operator_phone=PHONE,
            raw_message=_button_msg(BUTTON_REJECT_PRODUCT),
        )
    )

    # Generation must NOT have been triggered.
    assert gen_service.calls == 0
    assert result.generated_image_url is None

    # Reply should ask user to send the correct product.
    assert result.reply_text is not None
    assert "לא המוצר הנכון" in result.reply_text or "not the right product" in result.reply_text

    # Confirmation state should be cleared.
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        assert session_obj is not None
        assert session_obj.pending_question_type != PendingQuestionType.PRODUCT_CONFIRMATION

        draft = session_obj.current_draft
        assert draft is not None
        assert draft.awaiting_product_confirmation is False


async def test_no_confirmation_means_no_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the user never confirms (e.g., sends text instead of clicking a
    button), generation must NOT happen and the user is reprompted."""
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()
    wa_client = TrackingWhatsAppClient()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        ad_generation_service=gen_service,
        whatsapp_client=wa_client,
    )

    # Step 1: Product message -> confirmation prompt.
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-4a",
            operator_phone=PHONE,
            raw_message=_text_msg("מוצר אחד Cottage Cheese 19.90"),
        )
    )

    # Step 2: Send text instead of clicking a button.
    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-4b",
            operator_phone=PHONE,
            raw_message=_text_msg("hello"),
        )
    )

    # Generation must NOT have been triggered.
    assert gen_service.calls == 0
    assert result.generated_image_url is None

    # Should get a reprompt to use the buttons.
    assert result.reply_text is not None
    assert "כפתור" in result.reply_text or "button" in result.reply_text

    # Session should still be in PRODUCT_CONFIRMATION state.
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION


async def test_duplicate_confirm_does_not_trigger_duplicate_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Clicking confirm twice must NOT generate twice."""
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()
    wa_client = TrackingWhatsAppClient()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        ad_generation_service=gen_service,
        whatsapp_client=wa_client,
    )

    # Step 1: Product discovery -> confirmation.
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-5a",
            operator_phone=PHONE,
            raw_message=_text_msg("מוצר אחד Cottage Cheese 19.90"),
        )
    )

    # Step 2: First confirm -> triggers generation.
    result1 = await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-5b",
            operator_phone=PHONE,
            raw_message=_button_msg(BUTTON_CONFIRM_PRODUCT),
        )
    )
    assert gen_service.calls == 1
    assert result1.generated_image_url is not None

    # Step 3: Duplicate confirm (e.g., user clicks button again).
    result2 = await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-5c",
            operator_phone=PHONE,
            raw_message=_button_msg(BUTTON_CONFIRM_PRODUCT),
        )
    )

    # Generation must NOT fire again — the draft is already PREVIEW_READY
    # and pending_question_type is no longer PRODUCT_CONFIRMATION.
    assert gen_service.calls == 1
    assert result2.generated_image_url is None


async def test_image_during_confirmation_reprompts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Sending an image while awaiting confirmation should reprompt."""
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        ad_generation_service=gen_service,
    )

    # Step 1: Product discovery -> confirmation.
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-6a",
            operator_phone=PHONE,
            raw_message=_text_msg("מוצר אחד Cottage Cheese 19.90"),
        )
    )

    # Step 2: Send image instead of button.
    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-pc-6b",
            operator_phone=PHONE,
            raw_message={"type": "image", "image": {"id": "media-123"}},
        )
    )

    assert gen_service.calls == 0
    assert result.reply_text is not None
    assert "כפתור" in result.reply_text or "button" in result.reply_text
