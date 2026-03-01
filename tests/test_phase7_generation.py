import hashlib
import hmac
import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.ad_generation import (
    GenerationDraftInput,
    GenerationMode,
    GenerationSubmission,
    NanoBananaAdGenerationService,
    derive_aspect_ratio,
)
from adv_assistant.config import Settings
from adv_assistant.db.base import Base
from adv_assistant.db.enums import AdDraftStatus
from adv_assistant.db.models import AdDraft
from adv_assistant.db.repositories import AdDraftRepository, OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.llm_gateway import (
    ExtractedAdFields,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.main import create_app
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

    def __init__(self) -> None:
        self.calls = 0
        self.last_mode: GenerationMode | None = None

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

    async def close(self) -> None:
        return None


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_text(self, *, to_phone: str, message: str) -> None:
        self.messages.append((to_phone, message))

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


@pytest.fixture()
async def callback_app(tmp_path: Path) -> AsyncIterator:
    db_path = tmp_path / "phase7_callback.db"
    settings = Settings(
        app_name="adv-assistant-phase7-callback-test",
        database_url=f"sqlite+aiosqlite:///{db_path}",
        meta_verify_token="verify",
        meta_app_secret="meta-secret",
        nana_banana_callback_secret="phase7-callback-secret",
        tasks_mode="inline",
    )
    app = create_app(settings)
    async with app.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await app.router.startup()
    yield app
    await app.router.shutdown()


@pytest.fixture()
async def callback_client(callback_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=callback_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _seed_operator(
    factory: async_sessionmaker[AsyncSession], phone: str, language: str = "en"
) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(phone=phone, language=language, active=True)


def _sign_callback(secret: str, payload: dict[str, object]) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return body, f"sha256={signature}"


def test_derive_aspect_ratio_for_1920x1080() -> None:
    assert derive_aspect_ratio(width=1920, height=1080) == "16:9"


async def test_nano_banana_service_submits_expected_payload() -> None:
    draft_id = uuid.uuid4()
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url_path"] = request.url.path
        observed["authorization"] = request.headers.get("Authorization")
        observed["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"job_id": "job-server-1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = NanoBananaAdGenerationService(
        api_key="test-key",
        base_url="https://nano.example",
        callback_url="https://bot.example/callbacks/nano-banana",
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

    assert observed["url_path"] == "/v1/generate"
    assert observed["authorization"] == "Bearer test-key"
    body = observed["body"]
    assert isinstance(body, dict)
    assert body["model"] == "nanobanana-2"
    assert body["aspect_ratio"] == "16:9"
    assert body["metadata"]["draft_id"] == str(draft_id)
    assert first.job_id == "job-server-1"
    assert first.idempotency_key == second.idempotency_key
    await client.aclose()


async def test_pipeline_submits_generation_job_when_ready(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000701"
    await _seed_operator(session_factory, phone)
    fake_gateway = FakeGateway()
    fake_generation = FakeGenerationService()
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
    assert result.deterministic_action == "generation_submitted"
    assert result.reply_text is not None
    assert "generating your ad" in result.reply_text.lower()
    assert fake_gateway.reply_calls == 0
    assert fake_generation.calls == 1

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.GENERATING
        assert draft.generation_job_id == "job-123"


async def test_callback_completed_updates_draft_and_notifies_operator(
    callback_app,
    callback_client: AsyncClient,
) -> None:
    callback_app.state.whatsapp_client = FakeWhatsAppClient()
    phone = "+972500000702"

    async with session_scope(callback_app.state.session_factory) as session:
        await OperatorRepository(session).create(phone=phone, language="en", active=True)
        draft = await AdDraftRepository(session).create(
            operator_phone=phone,
            product_name="Milk",
            price=Decimal("10.90"),
            currency="ILS",
            status=AdDraftStatus.GENERATING,
            generation_job_id="job-callback-1",
        )
        draft_id = draft.id

    payload = {
        "job_id": "job-callback-1",
        "status": "completed",
        "output_image_url": "https://storage.googleapis.com/bucket/preview-1.png",
        "error_code": None,
        "error_message": None,
        "metadata": {"draft_id": str(draft_id), "operator_phone": phone},
    }
    body, signature = _sign_callback("phase7-callback-secret", payload)

    response = await callback_client.post(
        "/callbacks/nano-banana",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Nano-Banana-Signature-256": signature,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"status": "accepted"}

    async with session_scope(callback_app.state.session_factory) as session:
        updated_draft = await session.get(AdDraft, draft_id)
        assert updated_draft is not None
        assert updated_draft.status == AdDraftStatus.PREVIEW_READY
        assert (
            updated_draft.rendered_image_url
            == "https://storage.googleapis.com/bucket/preview-1.png"
        )
        assert (
            updated_draft.preview_reference_url
            == "https://storage.googleapis.com/bucket/preview-1.png"
        )

    sender = callback_app.state.whatsapp_client
    assert isinstance(sender, FakeWhatsAppClient)
    assert len(sender.messages) == 1
    assert sender.messages[0][0] == phone
    assert "preview is ready" in sender.messages[0][1].lower()


async def test_callback_rejects_invalid_signature(
    callback_client: AsyncClient,
) -> None:
    payload = {
        "job_id": "job-invalid-signature",
        "status": "failed",
        "output_image_url": None,
        "error_code": "upstream_error",
        "error_message": "provider failed",
        "metadata": {"draft_id": str(uuid.uuid4()), "operator_phone": "+972500000799"},
    }
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = await callback_client.post(
        "/callbacks/nano-banana",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Nano-Banana-Signature-256": "sha256=invalid",
        },
    )
    assert response.status_code == 401
