import uuid
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
    build_generation_prompt,
)
from adv_assistant.db.base import Base
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
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


class SequencedGateway:
    uses_external_llm = True

    def __init__(
        self,
        *,
        intents: list[Intent],
        ad_fields: list[ExtractedAdFields] | None = None,
        branding_fields: list[ExtractedBrandingFields] | None = None,
    ) -> None:
        self._intents = list(intents)
        self._ad_fields = list(ad_fields or [])
        self._branding_fields = list(branding_fields or [])
        self._last_intent = intents[-1] if intents else Intent.UNKNOWN

    async def classify_intent(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> IntentClassification:
        if self._intents:
            self._last_intent = self._intents.pop(0)
        return IntentClassification(intent=self._last_intent)

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
        if self._branding_fields:
            return self._branding_fields.pop(0)
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


class FakeGenerationService:
    enabled = True

    def __init__(self) -> None:
        self.submitted_drafts: list[GenerationDraftInput] = []

    async def submit_for_draft(
        self,
        *,
        draft: GenerationDraftInput,
        mode: GenerationMode,
        instruction_text: str,
        wamid: str,
        width: int,
        height: int,
        variant_slot: int | None = None,
    ) -> GenerationSubmission:
        self.submitted_drafts.append(draft)
        index = len(self.submitted_drafts)
        slot_suffix = f"-slot{variant_slot}" if variant_slot is not None else ""
        return GenerationSubmission(
            job_id=f"job-{index}{slot_suffix}",
            idempotency_key=f"key-{index}{slot_suffix}",
            mode=mode,
            request_payload={"draft_id": str(draft.draft_id), "prompt": "test-prompt"},
        )

    async def submit_variant_pair(
        self,
        *,
        draft: GenerationDraftInput,
        mode: GenerationMode,
        instruction_text: str,
        wamid: str,
        width: int,
        height: int,
    ) -> list[GenerationSubmission]:
        submissions = []
        for slot in (1, 2):
            sub = await self.submit_for_draft(
                draft=draft,
                mode=mode,
                instruction_text=instruction_text,
                wamid=wamid,
                width=width,
                height=height,
                variant_slot=slot,
            )
            submissions.append(sub)
        return submissions

    async def poll_variant_pair(
        self,
        *,
        submissions: list[GenerationSubmission],
    ) -> list[GenerationPollResult]:
        results = []
        for sub in submissions:
            results.append(await self.wait_for_completion(job_id=sub.job_id))
        return results

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
        return GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url=f"https://storage.example/generated/{job_id}.png",
        )

    async def close(self) -> None:
        return None


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "stage2_memory.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_operator(
    factory: async_sessionmaker[AsyncSession],
    phone: str,
    *,
    language: str = "he",
    store_type: str | None = None,
    creative_guidance: str | None = None,
) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(
            phone=phone,
            language=language,
            active=True,
            business_name="Test Biz",
            logo_url="https://example.com/logo.png",
            store_type=store_type,
            creative_guidance=creative_guidance,
        )


def test_build_generation_prompt_includes_operator_system_memory() -> None:
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972500000710",
        language="he",
        product_name="טחינה",
        price=Decimal("17.90"),
        currency="ILS",
        promo_text=None,
        ean=None,
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
        store_type="תבלינים ומזון יבש",
        creative_guidance="סגנון נקי, טיפוגרפיה בולטת למחיר",
    )

    prompt = build_generation_prompt(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="make it clean",
    )

    assert "Store Type: תבלינים ומזון יבש." in prompt
    assert "Creative Guidance: סגנון נקי, טיפוגרפיה בולטת למחיר." in prompt


async def test_set_branding_updates_operator_system_memory_fields(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000711"
    await _seed_operator(session_factory, phone)

    gateway = SequencedGateway(
        intents=[Intent.SET_BRANDING],
        branding_fields=[
            ExtractedBrandingFields(
                store_type="סופרמרקט שכונתי",
                creative_guidance="סגנון נקי עם מחיר בולט",
                preferred_language="עברית",
            )
        ],
    )
    processor = InboundTaskProcessor(session_factory, llm_gateway=gateway)

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-branding",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "סוג העסק שלי סופרמרקט שכונתי"}},
        )
    )

    assert result.status == "processed"
    assert result.reply_text is not None
    assert "עודכנו" in result.reply_text

    async with session_scope(session_factory) as session:
        operator = await OperatorRepository(session).get_by_phone(phone)
        assert operator is not None
        assert operator.store_type == "סופרמרקט שכונתי"
        assert operator.creative_guidance == "סגנון נקי עם מחיר בולט"
        assert operator.language == "he"


async def test_system_memory_persists_across_new_drafts_and_flows_to_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000712"
    await _seed_operator(
        session_factory,
        phone,
        store_type="חנות תבלינים",
        creative_guidance="גוונים חמים ופוקוס חזק על המוצר",
    )

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD, Intent.CREATE_AD],
        ad_fields=[
            ExtractedAdFields(product_name="פפריקה", price=Decimal("10.00"), currency="ILS"),
            ExtractedAdFields(product_name="כמון", price=Decimal("12.00"), currency="ILS"),
        ],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
    )

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-create-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לפפריקה"}},
        )
    )
    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-create-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לכמון"}},
        )
    )

    assert first.generated_image_url is not None
    assert second.generated_image_url is not None
    assert len(generation.submitted_drafts) == 4  # 2 generations × 2 variant slots

    first_draft = generation.submitted_drafts[0]
    second_draft = generation.submitted_drafts[2]  # second generation starts at index 2

    assert first_draft.draft_id != second_draft.draft_id
    assert first_draft.product_name == "פפריקה"
    assert second_draft.product_name == "כמון"

    assert first_draft.store_type == "חנות תבלינים"
    assert second_draft.store_type == "חנות תבלינים"
    assert first_draft.creative_guidance == "גוונים חמים ופוקוס חזק על המוצר"
    assert second_draft.creative_guidance == "גוונים חמים ופוקוס חזק על המוצר"

    assert second.reply_text is not None
    assert "רק שתדע שאם תספר לי מה סוג העסק" not in second.reply_text


async def test_generation_adds_followup_when_system_memory_is_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000713"
    await _seed_operator(session_factory, phone)

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[ExtractedAdFields(product_name="קולה", price=Decimal("6.00"), currency="ILS")],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-followup",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לקולה"}},
        )
    )

    assert result.status == "processed"
    assert result.reply_text is not None
    assert result.action_buttons_prompt is not None  # variant selection shown
    assert result.action_buttons is not None
    assert len(result.action_buttons) == 2  # A and B
    assert result.publish_buttons_prompt is None  # deferred until after variant selection


async def test_followup_questions_are_asked_one_by_one_then_regenerate_confirmation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000714"
    await _seed_operator(session_factory, phone)

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD, Intent.UNKNOWN, Intent.UNKNOWN],
        ad_fields=[
            ExtractedAdFields(product_name="קולה", price=Decimal("6.00"), currency="ILS"),
            ExtractedAdFields(price=Decimal("7.90"), currency="ILS"),
        ],
        branding_fields=[ExtractedBrandingFields()],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
    )

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-seq-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לקולה"}},
        )
    )
    assert first.reply_text is not None
    assert first.action_buttons is not None  # variant selection buttons shown
    assert len(generation.submitted_drafts) == 2  # two variant slots per generation

    # Select variant A to proceed to follow-up questions.
    variant_select = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-seq-1b",
            operator_phone=phone,
            raw_message={
                "type": "interactive",
                "interactive": {"button_reply": {"id": "select_variant_a"}},
            },
        )
    )
    assert variant_select.reply_text is not None
    assert "מה סוג העסק שלך" in variant_select.reply_text
    assert variant_select.publish_buttons_prompt is not None
    assert len(generation.submitted_drafts) == 2

    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-seq-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "סופרמרקט שכונתי"}},
        )
    )
    assert second.reply_text is not None
    assert "הנחיות כלליות לסגנון המודעות" in second.reply_text
    assert len(generation.submitted_drafts) == 2  # no new generation

    third = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-seq-3",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "סגנון נקי עם צבעים כחולים"}},
        )
    )
    assert third.reply_text is not None
    assert "רוצה שאפעיל עכשיו יצירת מודעה נוספת" in third.reply_text
    assert len(generation.submitted_drafts) == 2  # still no new generation

    fourth = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-seq-4",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "כן מחיר 7.90"}},
        )
    )
    assert fourth.reply_text is not None
    assert "התצוגה המקדימה מוכנה" in fourth.reply_text
    assert "רוצה שאפעיל עכשיו יצירת מודעה נוספת" not in fourth.reply_text
    assert len(generation.submitted_drafts) == 4  # 2 generations × 2 variant slots
    assert generation.submitted_drafts[2].price == Decimal("7.90")

    async with session_scope(session_factory) as session:
        operator = await OperatorRepository(session).get_by_phone(phone)
        assert operator is not None
        assert operator.store_type == "סופרמרקט שכונתי"
        assert operator.creative_guidance == "סגנון נקי עם צבעים כחולים"

        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(phone)
        assert session_obj is not None
        assert session_obj.pending_followup_question is None


async def test_followup_interrupt_create_ad_skips_pending_question_and_generates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000715"
    await _seed_operator(session_factory, phone)

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD, Intent.CREATE_AD],
        ad_fields=[
            ExtractedAdFields(product_name="מים", price=Decimal("5.00"), currency="ILS"),
            ExtractedAdFields(product_name="חלב", price=Decimal("8.00"), currency="ILS"),
        ],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
    )

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-interrupt-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה למים"}},
        )
    )
    assert first.reply_text is not None
    assert first.action_buttons is not None  # variant selection buttons shown

    # Interrupt with a new CREATE_AD while variant selection is pending.
    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-interrupt-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה חדשה לחלב"}},
        )
    )
    assert second.generated_image_url is not None
    assert len(generation.submitted_drafts) == 4  # 2 generations × 2 variant slots
    assert generation.submitted_drafts[2].product_name == "חלב"


async def test_regenerate_confirmation_unclear_answer_keeps_pending_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000716"
    await _seed_operator(
        session_factory,
        phone,
        store_type="סופר",
        creative_guidance="ניקיון וקריאות",
    )

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD, Intent.UNKNOWN],
        ad_fields=[ExtractedAdFields(product_name="מיץ", price=Decimal("9.00"), currency="ILS")],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
    )

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-unclear-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה למיץ"}},
        )
    )
    assert first.reply_text is not None
    assert first.action_buttons is not None  # variant selection buttons shown

    # Select variant A to proceed — follow-up (regenerate confirmation) should appear.
    variant_select = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-unclear-1b",
            operator_phone=phone,
            raw_message={
                "type": "interactive",
                "interactive": {"button_reply": {"id": "select_variant_a"}},
            },
        )
    )
    assert variant_select.reply_text is not None
    assert "רוצה שאפעיל עכשיו יצירת מודעה נוספת" in variant_select.reply_text

    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-unclear-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "אולי"}},
        )
    )
    assert second.reply_text is not None
    assert "רוצה שאפעיל עכשיו יצירת מודעה נוספת" in second.reply_text
    assert len(generation.submitted_drafts) == 2  # two variant slots per generation

    async with session_scope(session_factory) as session:
        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(phone)
        assert session_obj is not None
        assert session_obj.pending_followup_question == "regenerate_confirmation"


async def test_publish_buttons_sent_even_when_followup_pending(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000717"
    await _seed_operator(session_factory, phone)

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[ExtractedAdFields(product_name="בירה", price=Decimal("11.00"), currency="ILS")],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-no-publish-cta",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לבירה"}},
        )
    )

    assert result.generated_image_url is not None
    assert result.publish_buttons_prompt is None  # deferred until after variant selection
    assert result.action_buttons_prompt is not None  # variant selection buttons shown
    assert result.action_buttons is not None


async def test_regenerate_confirmation_decline_returns_publish_buttons(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000719"
    await _seed_operator(
        session_factory,
        phone,
        store_type="סופרמרקט",
        creative_guidance="סגנון נקי",
    )

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD, Intent.UNKNOWN],
        ad_fields=[
            ExtractedAdFields(
                product_name="שוקולד",
                price=Decimal("15.00"),
                currency="ILS",
            )
        ],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
    )

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-decline-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לשוקולד"}},
        )
    )
    assert first.reply_text is not None
    assert first.action_buttons is not None  # variant selection buttons shown

    # Select variant A to proceed to regenerate confirmation.
    variant_select = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-decline-1b",
            operator_phone=phone,
            raw_message={
                "type": "interactive",
                "interactive": {"button_reply": {"id": "select_variant_a"}},
            },
        )
    )
    assert variant_select.reply_text is not None
    assert "רוצה שאפעיל עכשיו יצירת מודעה נוספת" in variant_select.reply_text
    assert variant_select.publish_buttons_prompt is not None

    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-decline-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "לא"}},
        )
    )
    assert second.reply_text is not None
    assert "כשתרצה ליצור מודעה נוספת" in second.reply_text
    assert second.publish_buttons_prompt is not None


async def test_clear_request_removes_operator_fields_and_clears_pending_followup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000718"
    await _seed_operator(
        session_factory,
        phone,
        store_type="מינימרקט",
        creative_guidance="צבעים חמים",
    )

    async with session_scope(session_factory) as session:
        operator = await OperatorRepository(session).get_by_phone(phone)
        assert operator is not None
        await OperatorRepository(session).update_branding(
            phone,
            business_name="Super Shop",
            logo_url="https://example.com/logo.png",
            brand_colors=["#112233"],
        )
        await ConversationSessionRepository(session).create_or_update(
            operator_phone=phone,
            pending_followup_question="store_type",
        )

    gateway = SequencedGateway(intents=[Intent.UNKNOWN])
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=FakeGenerationService(),
    )
    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-stage2-clear-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "תמחק לוגו, סוג העסק והנחיות כלליות"}},
        )
    )

    assert result.reply_text is not None
    assert "ניקיתי את השדות הבאים" in result.reply_text
    assert "לוגו" in result.reply_text
    assert "סוג עסק" in result.reply_text

    async with session_scope(session_factory) as session:
        operator = await OperatorRepository(session).get_by_phone(phone)
        assert operator is not None
        assert operator.logo_url is None
        assert operator.store_type is None
        assert operator.creative_guidance is None
        assert operator.business_name == "Super Shop"
        assert operator.brand_colors == ["#112233"]

        session_obj = await ConversationSessionRepository(session).get_by_operator_phone(phone)
        assert session_obj is not None
        assert session_obj.pending_followup_question is None
