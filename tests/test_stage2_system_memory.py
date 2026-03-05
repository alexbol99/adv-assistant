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
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.llm_gateway import (
    ExtractedAdFields,
    ExtractedBrandingFields,
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
    ) -> GenerationSubmission:
        self.submitted_drafts.append(draft)
        index = len(self.submitted_drafts)
        return GenerationSubmission(
            job_id=f"job-{index}",
            idempotency_key=f"key-{index}",
            mode=mode,
            request_payload={"draft_id": str(draft.draft_id)},
        )

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
    assert len(generation.submitted_drafts) == 2

    first_draft = generation.submitted_drafts[0]
    second_draft = generation.submitted_drafts[1]

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
    assert "רק שתדע שאם תספר לי מה סוג העסק" in result.reply_text
    assert "אם יש לך הנחיות כלליות לסגנון המודעות" in result.reply_text
