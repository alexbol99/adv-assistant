"""End-to-end test: "תייצר לי פרסומת לחומוס אחלה"

Traces the full pipeline from the moment the operator sends the text
message, through product confirmation, to ad generation and preview.
"""

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
from adv_assistant.db.repositories import (
    AdDraftRepository,
    ConversationSessionRepository,
    OperatorRepository,
)
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
from adv_assistant.enrichment import EnrichedProduct, ProviderChainEnrichmentService
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio

PHONE = "+972548006499"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class HummusGateway:
    """LLM gateway that simulates responses for 'חומוס אחלה'."""

    uses_external_llm = True

    async def classify_intent(
        self, *, message_text: str, language: str, history: list[dict[str, str]]
    ) -> IntentClassification:
        return IntentClassification(intent=Intent.CREATE_AD)

    async def extract_ad_fields(
        self, *, message_text: str, language: str, history: list[dict[str, str]]
    ) -> ExtractedAdFields:
        return ExtractedAdFields(
            product_name="חומוס אחלה",
            price=None,        # user didn't mention price
            currency=None,
            promo_text=None,
            ean=None,           # no barcode in text
            photo_url=None,
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
    """Records generation calls and returns a fake image URL."""

    enabled = True

    def __init__(self) -> None:
        self.calls = 0
        self.submitted_drafts: list[GenerationDraftInput] = []

    async def submit_for_draft(
        self, *, draft: GenerationDraftInput, mode: GenerationMode,
        instruction_text: str, wamid: str, width: int, height: int,
    ) -> GenerationSubmission:
        self.calls += 1
        self.submitted_drafts.append(draft)
        return GenerationSubmission(
            job_id="job-hummus-1",
            idempotency_key="key-hummus-1",
            mode=mode,
            request_payload={"draft_id": str(draft.draft_id)},
        )

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
        return GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url=f"https://storage.example/generated/{job_id}.png",
        )

    async def close(self) -> None:
        pass


class FakeSerperImageSearchProvider:
    """Fake image search that returns a product image for any query."""

    @property
    def source(self) -> str:
        return "serper_images"

    async def lookup_by_name(
        self, *, product_name: str, language: str
    ) -> EnrichedProduct:
        return EnrichedProduct(
            image_url=f"https://images.example/search/{product_name}.jpg",
            source=self.source,
        )

    async def close(self) -> None:
        pass


class TrackingWhatsAppClient:
    """Records every outbound WhatsApp call."""

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
# Helpers
# ---------------------------------------------------------------------------


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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "e2e_hummus.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_operator(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(
            phone=PHONE,
            language="he",
            active=True,
            business_name="מכולת השכונה",
            logo_url="https://example.com/logo.png",
            store_type="מכולת",
            creative_guidance="סגנון נקי עם צבעים חמים",
        )


# ---------------------------------------------------------------------------
# End-to-end test
# ---------------------------------------------------------------------------


async def test_e2e_hummus_achla_full_flow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Full flow: text → confirmation → confirm button → generation → preview.

    Input: "תייצר לי פרסומת לחומוס אחלה"

    The operator has store_type and creative_guidance pre-set, so after
    generation the only missing field is price.  The flow should:
    1. Send confirmation buttons with product name in the body.
    2. On confirm, generate the ad.
    3. Return generated_image_url + reply with price followup.
    """
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()
    wa_client = TrackingWhatsAppClient()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=HummusGateway(),
        ad_generation_service=gen_service,
        whatsapp_client=wa_client,
    )

    # =================================================================
    # MESSAGE 1: User sends "תייצר לי פרסומת לחומוס אחלה"
    # =================================================================
    result1 = await processor.process(
        InboundTaskPayload(
            wamid="wamid-hummus-1",
            operator_phone=PHONE,
            raw_message=_text_msg("תייצר לי פרסומת לחומוס אחלה"),
        )
    )

    # -- ProcessInboundResult assertions --
    assert result1.status == "processed"
    assert result1.duplicate is False
    assert result1.session_created is True
    assert result1.draft_created is True
    assert result1.llm_used is True
    assert result1.intent == "create_ad"
    assert result1.deterministic_action == "product_confirmation_sent"
    assert result1.generated_image_url is None       # NOT yet generated
    assert result1.publish_buttons_prompt is None

    # -- reply_text includes product name --
    assert result1.reply_text is not None
    assert "מצאתי את המוצר הזה" in result1.reply_text
    assert "חומוס אחלה" in result1.reply_text

    # -- Generation must NOT have been called --
    assert gen_service.calls == 0

    # -- WhatsApp: confirmation buttons were sent --
    assert len(wa_client.sent_buttons) == 1
    btn_call = wa_client.sent_buttons[0]
    assert btn_call["to_phone"] == PHONE
    assert "חומוס אחלה" in btn_call["body_text"]
    button_ids = [b[0] for b in btn_call["buttons"]]
    assert button_ids == [BUTTON_CONFIRM_PRODUCT, BUTTON_REJECT_PRODUCT]
    # No image header (no EAN → no enrichment → no image URL)
    assert btn_call["header_image_url"] is None

    # -- No other WhatsApp messages (no generation-in-progress, no image) --
    assert len(wa_client.sent_texts) == 0
    assert len(wa_client.sent_images) == 0

    # -- DB: session is in PRODUCT_CONFIRMATION state --
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION
        assert session_obj.pending_question_context is not None
        assert session_obj.pending_question_context["product_name"] == "חומוס אחלה"
        assert session_obj.last_user_intent_hint == "create_ad"

        draft = session_obj.current_draft
        assert draft is not None
        assert draft.product_name == "חומוס אחלה"
        assert draft.price is None
        assert draft.awaiting_product_confirmation is True
        assert draft.status == AdDraftStatus.DRAFT
        # No enrichment (no EAN found)
        assert draft.enriched_image_url is None
        assert draft.enriched_product_name is None

    # =================================================================
    # MESSAGE 2: User taps "זה המוצר שלי" (confirm button)
    # =================================================================
    result2 = await processor.process(
        InboundTaskPayload(
            wamid="wamid-hummus-2",
            operator_phone=PHONE,
            raw_message=_button_msg(BUTTON_CONFIRM_PRODUCT),
        )
    )

    # -- ProcessInboundResult assertions --
    assert result2.status == "processed"
    assert result2.duplicate is False
    assert result2.session_created is False
    assert result2.draft_created is False
    assert result2.llm_used is False     # button handler is deterministic
    assert result2.intent == "product_confirmed"
    assert result2.deterministic_action == "generation_completed"
    assert result2.generated_image_url is not None
    assert "storage.example/generated/job-hummus-1.png" in result2.generated_image_url
    assert result2.publish_buttons_prompt is not None
    assert "CMS" in result2.publish_buttons_prompt

    # -- reply_text: preview ready + price followup --
    assert result2.reply_text is not None
    assert "התצוגה המקדימה מוכנה" in result2.reply_text
    # Price was None, so the first followup should ask for price
    assert "מה המחיר המדויק" in result2.reply_text

    # -- Generation was called exactly once --
    assert gen_service.calls == 1
    submitted = gen_service.submitted_drafts[0]
    assert submitted.product_name == "חומוס אחלה"
    assert submitted.language == "he"
    assert submitted.store_type == "מכולת"
    assert submitted.creative_guidance == "סגנון נקי עם צבעים חמים"

    # -- WhatsApp: "in progress" text was sent before generation --
    assert len(wa_client.sent_texts) == 1
    assert "בתהליך יצירה" in wa_client.sent_texts[0]["message"]

    # -- DB: draft is PREVIEW_READY, confirmation state cleared --
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.NONE
        assert session_obj.pending_followup_question == "price"

        draft = session_obj.current_draft
        assert draft is not None
        assert draft.status == AdDraftStatus.PREVIEW_READY
        assert draft.awaiting_product_confirmation is False
        assert draft.rendered_image_url is not None
        assert "job-hummus-1" in draft.rendered_image_url
        assert draft.product_name == "חומוס אחלה"


async def test_e2e_hummus_achla_reject_flow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Rejection flow: text → confirmation → reject button → correction prompt.

    When the user clicks "זה לא המוצר שלי", generation must not happen
    and the user is asked to send the correct product name.
    """
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()
    wa_client = TrackingWhatsAppClient()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=HummusGateway(),
        ad_generation_service=gen_service,
        whatsapp_client=wa_client,
    )

    # MESSAGE 1: Product text → confirmation.
    result1 = await processor.process(
        InboundTaskPayload(
            wamid="wamid-hummus-rej-1",
            operator_phone=PHONE,
            raw_message=_text_msg("תייצר לי פרסומת לחומוס אחלה"),
        )
    )
    assert result1.deterministic_action == "product_confirmation_sent"
    assert "חומוס אחלה" in result1.reply_text

    # MESSAGE 2: User taps "זה לא המוצר שלי" (reject).
    result2 = await processor.process(
        InboundTaskPayload(
            wamid="wamid-hummus-rej-2",
            operator_phone=PHONE,
            raw_message=_button_msg(BUTTON_REJECT_PRODUCT),
        )
    )

    # -- Generation must NOT have happened --
    assert gen_service.calls == 0
    assert result2.generated_image_url is None

    # -- Reply asks user to send correct product --
    assert result2.reply_text is not None
    assert "לא המוצר הנכון" in result2.reply_text

    # -- DB: confirmation state cleared --
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        assert session_obj is not None
        assert session_obj.pending_question_type != PendingQuestionType.PRODUCT_CONFIRMATION

        draft = session_obj.current_draft
        assert draft is not None
        assert draft.awaiting_product_confirmation is False
        assert draft.status == AdDraftStatus.DRAFT  # still DRAFT, not generated


async def test_e2e_hummus_achla_text_during_confirmation_reprompts(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the user sends text while waiting for confirmation buttons,
    the bot should reprompt to use the buttons."""
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=HummusGateway(),
        ad_generation_service=gen_service,
    )

    # MESSAGE 1: Product text → confirmation.
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-hummus-text-1",
            operator_phone=PHONE,
            raw_message=_text_msg("תייצר לי פרסומת לחומוס אחלה"),
        )
    )

    # MESSAGE 2: User sends text instead of clicking a button.
    result2 = await processor.process(
        InboundTaskPayload(
            wamid="wamid-hummus-text-2",
            operator_phone=PHONE,
            raw_message=_text_msg("שלום, מה קורה?"),
        )
    )

    # -- Generation must NOT happen --
    assert gen_service.calls == 0

    # -- Reprompt to use buttons --
    assert result2.reply_text is not None
    assert "כפתור" in result2.reply_text
    assert result2.deterministic_action == "product_confirmation_reprompt"

    # -- Still in confirmation state --
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION


async def test_e2e_hummus_achla_with_image_search(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When Serper image search is configured, the confirmation message
    should include a product image even without an EAN barcode."""
    await _seed_operator(session_factory)
    gen_service = FakeGenerationService()
    wa_client = TrackingWhatsAppClient()

    enrichment_service = ProviderChainEnrichmentService(
        providers=[],
        image_search_provider=FakeSerperImageSearchProvider(),
    )

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=HummusGateway(),
        ad_generation_service=gen_service,
        whatsapp_client=wa_client,
        enrichment_service=enrichment_service,
    )

    # MESSAGE 1: User sends text (no barcode).
    result1 = await processor.process(
        InboundTaskPayload(
            wamid="wamid-hummus-img-1",
            operator_phone=PHONE,
            raw_message=_text_msg("תייצר לי פרסומת לחומוס אחלה"),
        )
    )

    assert result1.deterministic_action == "product_confirmation_sent"
    assert "חומוס אחלה" in result1.reply_text

    # -- WhatsApp: confirmation buttons should include the image --
    assert len(wa_client.sent_buttons) == 1
    btn_call = wa_client.sent_buttons[0]
    assert btn_call["header_image_url"] is not None
    assert "images.example/search/" in btn_call["header_image_url"]

    # -- DB: enriched_image_url should be set --
    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(PHONE)
        draft = session_obj.current_draft
        assert draft is not None
        assert draft.enriched_image_url is not None
        assert "images.example/search/" in draft.enriched_image_url
        assert draft.enrichment_source == "serper_images"
