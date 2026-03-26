"""E2E tests for the full Conversation Flow V1 pipeline.

Covers:
- Full flow: create ad → generate → select variant → publish
- Already-enough-info → direct generation path
- Interrupt: new CREATE_AD during variant selection
- Publish action resets the active draft
"""

from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant import creative_brief_planner
from adv_assistant.ad_generation import (
    GenerationDraftInput,
    GenerationMode,
    GenerationPollResult,
    GenerationSubmission,
    NanoBananaJobStatus,
)
from adv_assistant.cms_cityscreen import CMSPublishResult
from adv_assistant.db.base import Base
from adv_assistant.db.enums import (
    AdDraftStatus,
    AdRequestType,
    ClassificationStatus,
    PendingQuestionType,
)
from adv_assistant.db.models import AdDraft, ConversationSession, PublishedAd
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.llm_gateway import (
    BUTTON_CONFIRM_PRODUCT_SELECTION,
    BUTTON_CONFIRM_PUBLISH,
    BUTTON_REJECT_PRODUCT_SELECTION,
    BUTTON_SELECT_VARIANT_A,
    BUTTON_SELECT_VARIANT_B,
    ExtractedAdFields,
    ExtractedBrandingFields,
    ExtractedProductQuery,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.media_ingest import IngestedOperatorPhoto
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


# ── Fakes ──────────────────────────────────────────────────────────────


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
        self, *, message_text: str, language: str, history: list[dict[str, str]]
    ) -> IntentClassification:
        if self._intents:
            self._last_intent = self._intents.pop(0)
        return IntentClassification(intent=self._last_intent)

    async def extract_ad_fields(
        self, *, message_text: str, language: str, history: list[dict[str, str]]
    ) -> ExtractedAdFields:
        if self._ad_fields:
            return self._ad_fields.pop(0)
        return ExtractedAdFields()

    async def extract_branding_fields(
        self, *, message_text: str, language: str
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

    async def plan_creative_brief(
        self,
        *,
        context: creative_brief_planner.CreativeBriefPlannerContext,
        questions_asked_so_far: int,
        missing_mandatory_fields: list[str],
    ) -> creative_brief_planner.CreativeBriefPlannerOutput:
        current_state = creative_brief_planner.CurrentBriefState(
            scene="Clean product hero shot in a supermarket aisle",
        )
        return creative_brief_planner.CreativeBriefPlannerOutput(
            decision=creative_brief_planner.CreativeBriefDecision.BRIEF_READY,
            current_brief_state=current_state,
            missing_dimensions=[],
            final_brief=None,
            internal_notes="sequenced_gateway_brief_ready",
            confidence=0.8,
        )


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
        slot_suffix = f"-slot{variant_slot}" if variant_slot else ""
        return GenerationSubmission(
            job_id=f"job-{index}{slot_suffix}",
            idempotency_key=f"key-{index}{slot_suffix}",
            mode=mode,
            request_payload={
                "draft_id": str(draft.draft_id),
                "prompt": "test-prompt",
            },
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
        self, *, submissions: list[GenerationSubmission]
    ) -> list[GenerationPollResult]:
        results = []
        for _sub in submissions:
            results.append(
                GenerationPollResult(
                    status=NanoBananaJobStatus.COMPLETED,
                    output_image_url=("https://storage.googleapis.com/media/preview.png"),
                )
            )
        return results

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
        return GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url=("https://storage.googleapis.com/media/preview.png"),
        )


class FakeCMSPublisher:
    def __init__(self) -> None:
        self.enabled = True
        self.calls = 0

    async def publish_generated_image(
        self,
        *,
        image_url: str,
        title: str,
        campaign_id: int | None = None,
        playlist_id: int | None = None,
    ) -> CMSPublishResult:
        self.calls += 1
        return CMSPublishResult(file_id=100, advertisement_id=200, slot_id=300)

    async def close(self) -> None:
        return None


class StaticPhotoIngestor:
    async def ingest_whatsapp_image(self, *, media_id: str) -> IngestedOperatorPhoto:
        return IngestedOperatorPhoto(
            public_url="https://storage.googleapis.com/media/image-first-e2e.jpg",
            object_name="operator-photos/image-first-e2e.jpg",
            content_type="image/jpeg",
            content=b"jpeg-bytes",
        )

    async def ingest_external_image_url(self, *, image_url: str) -> IngestedOperatorPhoto:
        return IngestedOperatorPhoto(
            public_url="https://storage.googleapis.com/media/image-first-e2e-rehost.jpg",
            object_name="operator-photos/image-first-e2e-rehost.jpg",
            content_type="image/jpeg",
            content=b"jpeg-bytes",
        )

    async def close(self) -> None:
        return None


# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
async def session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "e2e.db"
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
    store_type: str | None = None,
    creative_guidance: str | None = None,
    campaign_id: int = 157,
    playlist_id: int = 139,
) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(
            phone=phone,
            active=True,
            business_name="Test Biz",
            logo_url="https://example.com/logo.png",
            cms_campaign_id=campaign_id,
            cms_playlist_id=playlist_id,
            store_type=store_type,
            creative_guidance=creative_guidance,
        )


def _button_payload(button_id: str) -> dict:
    return {
        "type": "interactive",
        "interactive": {"button_reply": {"id": button_id}},
    }


# ── Tests ──────────────────────────────────────────────────────────────


async def test_full_v1_flow_create_generate_select_publish(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Full happy path: create ad → generate → select variant A → publish."""
    phone = "+972500000901"
    await _seed_operator(
        session_factory,
        phone,
        store_type="סופרמרקט",
        creative_guidance="נקי",
    )

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD, Intent.PUBLISH_AD],
        ad_fields=[
            ExtractedAdFields(
                product_name="חלב",
                price=Decimal("6.90"),
                currency="ILS",
            )
        ],
    )
    generation = FakeGenerationService()
    cms = FakeCMSPublisher()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
        cms_publisher=cms,
    )

    # Step 1: Create ad → generation → variant selection buttons.
    gen_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לחלב"}},
        )
    )
    assert gen_result.generated_image_url is not None
    assert gen_result.variant_image_urls is not None
    assert len(gen_result.variant_image_urls) == 2
    assert gen_result.action_buttons is not None
    assert gen_result.publish_buttons_prompt is None
    assert len(generation.submitted_drafts) == 2

    # Step 2: Select variant A → publish buttons shown.
    select_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-2",
            operator_phone=phone,
            raw_message=_button_payload(BUTTON_SELECT_VARIANT_A),
        )
    )
    assert select_result.deterministic_action == "variant_selected"
    assert select_result.publish_buttons_prompt is not None

    # Step 3: Confirm publish.
    publish_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-3",
            operator_phone=phone,
            raw_message=_button_payload(BUTTON_CONFIRM_PUBLISH),
        )
    )
    assert publish_result.deterministic_action == "confirm_publish"
    assert cms.calls == 1

    # Verify DB state: published draft is kept and a fresh draft becomes active.
    async with session_scope(session_factory) as session:
        drafts = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .all()
        )
        assert len(drafts) == 2
        published_draft = next((d for d in drafts if d.status == AdDraftStatus.PUBLISHED), None)
        assert published_draft is not None
        assert published_draft.selected_variant_id is not None
        assert published_draft.selected_round_id is not None

        published = (
            (
                await session.execute(
                    select(PublishedAd).where(PublishedAd.ad_draft_id == published_draft.id)
                )
            )
            .scalars()
            .first()
        )
        assert published is not None

        session_obj = (
            (
                await session.execute(
                    select(ConversationSession).where(ConversationSession.operator_phone == phone)
                )
            )
            .scalars()
            .first()
        )
        assert session_obj is not None
        assert session_obj.current_draft_id is not None
        active_draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert active_draft is not None
        assert active_draft.status == AdDraftStatus.DRAFT
        assert active_draft.product_name is None


async def test_already_enough_info_generates_directly(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When all required info is provided, generation starts immediately."""
    phone = "+972500000902"
    await _seed_operator(
        session_factory,
        phone,
        store_type="סופר",
        creative_guidance="נקי",
    )

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[
            ExtractedAdFields(
                product_name="מיץ",
                price=Decimal("9.90"),
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

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-direct",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה למיץ"}},
        )
    )
    assert result.generated_image_url is not None
    assert result.variant_image_urls is not None
    assert len(result.variant_image_urls) == 2
    assert result.action_buttons is not None  # variant selection
    assert len(generation.submitted_drafts) == 2  # 2 variant slots


async def test_image_first_message_enters_product_confirmation_flow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000906"
    await _seed_operator(session_factory, phone)

    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=SequencedGateway(intents=[]),
        operator_photo_ingestor=StaticPhotoIngestor(),
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-image-first",
            operator_phone=phone,
            raw_message={"type": "image", "image": {"id": "media-image-first"}},
        )
    )

    assert result.deterministic_action == "product_confirmation_requested"
    assert result.generated_image_url == "https://storage.googleapis.com/media/image-first-e2e.jpg"
    assert result.publish_buttons_prompt is None
    assert result.action_buttons_prompt is not None
    assert result.action_buttons == [
        (BUTTON_CONFIRM_PRODUCT_SELECTION, "כן, זה המוצר"),
        (BUTTON_REJECT_PRODUCT_SELECTION, "לא, מוצר אחר"),
    ]

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.photo_url == "https://storage.googleapis.com/media/image-first-e2e.jpg"
        assert draft.request_type == AdRequestType.SINGLE_PRODUCT
        assert draft.classification_status == ClassificationStatus.RESOLVED
        assert draft.is_classification_resolved is True
        assert draft.awaiting_product_confirmation is True

        session_obj = (
            (
                await session.execute(
                    select(ConversationSession).where(ConversationSession.operator_phone == phone)
                )
            )
            .scalars()
            .first()
        )
        assert session_obj is not None
        assert session_obj.pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION


async def test_select_variant_b_sets_correct_variant(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Selecting variant B stores the correct variant_id."""
    phone = "+972500000903"
    await _seed_operator(
        session_factory,
        phone,
        store_type="מכולת",
        creative_guidance="צבעוני",
    )

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[
            ExtractedAdFields(
                product_name="ביסלי",
                price=Decimal("4.50"),
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

    # Generate
    await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-vb-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לביסלי"}},
        )
    )

    # Select variant B
    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-vb-2",
            operator_phone=phone,
            raw_message=_button_payload(BUTTON_SELECT_VARIANT_B),
        )
    )
    assert result.deterministic_action == "variant_selected"
    assert "B" in result.reply_text
    assert result.publish_buttons_prompt is not None

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.selected_variant_id is not None


async def test_interrupt_create_ad_during_variant_selection(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A new CREATE_AD during variant selection starts a new draft."""
    phone = "+972500000904"
    await _seed_operator(
        session_factory,
        phone,
        store_type="סופר",
        creative_guidance="נקי",
    )

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD, Intent.CREATE_AD],
        ad_fields=[
            ExtractedAdFields(
                product_name="מים",
                price=Decimal("3.00"),
                currency="ILS",
            ),
            ExtractedAdFields(
                product_name="קולה",
                price=Decimal("7.00"),
                currency="ILS",
            ),
        ],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
    )

    # First ad generates.
    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-int-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה למים"}},
        )
    )
    assert first.action_buttons is not None  # variant selection

    # Interrupt with second CREATE_AD — should generate new ad.
    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-int-2",
            operator_phone=phone,
            raw_message={
                "type": "text",
                "text": {"body": "מודעה חדשה לקולה"},
            },
        )
    )
    assert second.generated_image_url is not None
    # 4 submitted: 2 variants per generation × 2 generations
    assert len(generation.submitted_drafts) == 4
    assert generation.submitted_drafts[2].product_name == "קולה"


async def test_pipeline_v1_disabled_returns_unavailable(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """When pipeline_v1_enabled=False, processing returns unavailable."""
    phone = "+972500000905"
    await _seed_operator(session_factory, phone, store_type="סופר")

    gateway = SequencedGateway(
        intents=[Intent.CREATE_AD],
        ad_fields=[
            ExtractedAdFields(
                product_name="חלב",
                price=Decimal("6.00"),
                currency="ILS",
            )
        ],
    )
    generation = FakeGenerationService()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=gateway,
        ad_generation_service=generation,
        pipeline_v1_enabled=False,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-e2e-disabled",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לחלב"}},
        )
    )
    assert result.reply_text is not None
    assert "temporarily unavailable" in result.reply_text.lower()
    assert result.generated_image_url is None
    assert len(generation.submitted_drafts) == 0  # no generation happened
