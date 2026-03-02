import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.ad_generation import (
    GenerationDraftInput,
    GenerationMode,
    GenerationPollResult,
    GenerationSubmission,
    NanoBananaAdGenerationService,
    NanoBananaJobStatus,
    derive_aspect_ratio,
)
from adv_assistant.db.base import Base
from adv_assistant.db.enums import AdDraftStatus
from adv_assistant.db.models import AdDraft
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.llm_gateway import (
    ExtractedAdFields,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


class FakeGateway:
    uses_external_llm = True

    def __init__(self) -> None:
        self.reply_calls = 0

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
        return ExtractedAdFields(
            product_name="Cottage Cheese",
            price=Decimal("19.90"),
            currency="ILS",
            promo_text="Fresh and tasty",
        )

    async def generate_reply(
        self,
        *,
        intent: Intent,
        message_text: str,
        language: str,
        extracted_fields: ExtractedAdFields | None,
    ) -> ReplyGeneration:
        self.reply_calls += 1
        return ReplyGeneration(reply_text="LLM reply fallback")


class FakeGenerationService:
    enabled = True

    def __init__(
        self,
        *,
        poll_result: GenerationPollResult,
    ) -> None:
        self.calls = 0
        self.last_mode: GenerationMode | None = None
        self.wait_calls = 0
        self._poll_result = poll_result

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
        self.last_mode = mode
        return GenerationSubmission(
            job_id="job-123",
            idempotency_key="fixed-key",
            mode=mode,
            request_payload={"draft_id": str(draft.draft_id)},
        )

    async def wait_for_completion(self, *, job_id: str) -> GenerationPollResult:
        self.wait_calls += 1
        return self._poll_result

    async def close(self) -> None:
        return None


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "phase7.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_operator(
    factory: async_sessionmaker[AsyncSession], phone: str, *, language: str = "en"
) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(phone=phone, language=language, active=True)


def test_derive_aspect_ratio_for_1920x1080() -> None:
    assert derive_aspect_ratio(width=1920, height=1080) == "16:9"


async def test_nano_banana_service_submits_expected_payload() -> None:
    draft_id = uuid.uuid4()
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url_path"] = request.url.path
        observed["authorization"] = request.headers.get("Authorization")
        observed["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            status_code=200,
            json={"code": 200, "msg": "ok", "data": {"taskId": "job-server-1"}},
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = NanoBananaAdGenerationService(
        api_key="test-key",
        api_url="https://nano.example/api/v1/nanobanana/generate-2",
        status_api_url_template="https://nano.example/api/v1/nanobanana/jobs/{job_id}",
        client=client,
    )
    draft = GenerationDraftInput(
        draft_id=draft_id,
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean="7290004127326",
        photo_url=None,
        enriched_brand="תנובה",
        enriched_category="Dairy",
        enriched_description="גבינה לבנה",
        preview_reference_url=None,
        rendered_image_url=None,
    )

    first = await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad",
        wamid="wamid-1",
        width=1920,
        height=1080,
    )
    second = await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad",
        wamid="wamid-1",
        width=1920,
        height=1080,
    )

    assert observed["url_path"] == "/api/v1/nanobanana/generate-2"
    assert observed["authorization"] == "Bearer test-key"
    body = observed["body"]
    assert isinstance(body, dict)
    assert body["type"] == "TEXTTOIAMGE"
    assert body["numImages"] == 1
    assert body["watermark"] is None
    assert body["model"] == "nanobanana-2"
    assert body["aspect_ratio"] == "16:9"
    assert body["metadata"]["draft_id"] == str(draft_id)
    assert first.job_id == "job-server-1"
    assert first.idempotency_key == second.idempotency_key
    await client.aclose()


async def test_nano_banana_service_polls_until_completion() -> None:
    state = {"status_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if (
            request.url.path.endswith("/record-info")
            and request.url.params.get("taskId") == "job-server-2"
        ):
            state["status_calls"] += 1
            if state["status_calls"] < 3:
                return httpx.Response(status_code=200, json={"successFlag": 0})
            return httpx.Response(
                status_code=200,
                json={
                    "successFlag": 1,
                    "response": {
                        "resultImageUrl": "https://storage.googleapis.com/media/preview.png"
                    },
                },
            )
        return httpx.Response(status_code=404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = NanoBananaAdGenerationService(
        api_key="test-key",
        base_url="https://nano.example/api/v1/nanobanana",
        poll_initial_seconds=0.01,
        poll_max_seconds=0.02,
        poll_timeout_seconds=1,
        client=client,
    )

    result = await service.wait_for_completion(job_id="job-server-2")

    assert result.status == NanoBananaJobStatus.COMPLETED
    assert result.output_image_url == "https://storage.googleapis.com/media/preview.png"
    assert state["status_calls"] == 3
    await client.aclose()


async def test_pipeline_submits_generation_job_and_sets_preview_ready(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000701"
    await _seed_operator(session_factory, phone)
    fake_gateway = FakeGateway()
    fake_generation = FakeGenerationService(
        poll_result=GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://storage.googleapis.com/media/preview-1.png",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=fake_gateway,
        ad_generation_service=fake_generation,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-generate",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad for cottage 19.90"}},
        )
    )

    assert result.status == "processed"
    assert result.deterministic_action == "generation_completed"
    assert result.reply_text is not None
    assert "preview is ready" in result.reply_text.lower()
    assert fake_gateway.reply_calls == 0
    assert fake_generation.calls == 1
    assert fake_generation.wait_calls == 1

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.PREVIEW_READY
        assert draft.generation_job_id == "job-123"
        assert draft.rendered_image_url == "https://storage.googleapis.com/media/preview-1.png"


async def test_pipeline_generation_failure_returns_fallback_message(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000702"
    await _seed_operator(session_factory, phone)
    fake_gateway = FakeGateway()
    fake_generation = FakeGenerationService(
        poll_result=GenerationPollResult(
            status=NanoBananaJobStatus.FAILED,
            error_code="provider_error",
            error_message="failed upstream",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=fake_gateway,
        ad_generation_service=fake_generation,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-generate-fail",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad for cottage 19.90"}},
        )
    )

    assert result.status == "processed"
    assert result.deterministic_action == "generation_failed"
    assert result.reply_text is not None
    assert "temporary generation service issue" in result.reply_text.lower()

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.DRAFT
