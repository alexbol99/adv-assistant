import base64
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
    AdGenerationError,
    GeminiFlashImageAdGenerationService,
    GenerationDraftInput,
    GenerationMode,
    GenerationPollResult,
    GenerationSubmission,
    NanoBananaAdGenerationService,
    NanoBananaJobStatus,
    build_generation_prompt,
    derive_aspect_ratio,
)
from adv_assistant.db.base import Base
from adv_assistant.db.enums import AdDraftStatus, PendingQuestionType
from adv_assistant.db.models import AdDraft, AuditEvent, ConversationSession
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.enrichment import EnrichedProduct
from adv_assistant.llm_gateway import (
    BUTTON_CONFIRM_PRODUCT_SELECTION,
    BUTTON_REJECT_PRODUCT_SELECTION,
    ExtractedAdFields,
    ExtractedProductQuery,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.media_ingest import IngestedOperatorPhoto
from adv_assistant.media_store import MediaUpload
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.product_resolution_models import ProductResolutionResult, SelectedProductResult
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
        self.reply_calls += 1
        return ReplyGeneration(reply_text="LLM reply fallback")


class FakeGatewayNoPrice(FakeGateway):
    async def extract_ad_fields(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> ExtractedAdFields:
        return ExtractedAdFields(
            product_name="Cottage Cheese",
            currency="ILS",
            promo_text="Fresh and tasty",
        )


class FakeGatewayNoProductName(FakeGateway):
    async def extract_ad_fields(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> ExtractedAdFields:
        return ExtractedAdFields(
            price=Decimal("19.90"),
            currency="ILS",
            promo_text="Fresh and tasty",
        )


class FakeGatewayBrandConflict(FakeGateway):
    async def extract_ad_fields(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> ExtractedAdFields:
        return ExtractedAdFields(
            product_name="White Cheese",
            product_brand="Private Label",
            price=Decimal("14.90"),
            currency="ILS",
            ean="7290001234567",
        )


class StaticEnrichmentService:
    def __init__(self, enriched: EnrichedProduct | None) -> None:
        self._enriched = enriched

    async def decode_ean_from_image(self, image_bytes: bytes) -> str | None:
        return None

    async def enrich_by_ean(self, *, ean: str, language: str) -> EnrichedProduct | None:
        return self._enriched

    async def close(self) -> None:
        return None


class FakeResolvedProductResolutionService:
    enabled = True

    async def resolve(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ProductResolutionResult:
        return ProductResolutionResult(
            status="resolved",
            brand="Magnum",
            product_query="גלידת מגנום",
            raw_user_text=message_text,
            selected_result=SelectedProductResult(
                title="גלידת מגנום",
                description="Magnum ice cream",
                image_url="https://example.com/magnum.png",
                product_url="https://example.com/magnum",
                source="fake",
                search_method="retailer",
            ),
        )

    async def close(self) -> None:
        return None


class FakeResolvedProductResolutionServiceUnsafeImage:
    enabled = True

    async def resolve(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ProductResolutionResult:
        return ProductResolutionResult(
            status="resolved",
            brand="Magnum",
            product_query="גלידת מגנום",
            raw_user_text=message_text,
            selected_result=SelectedProductResult(
                title="גלידת מגנום",
                description="Magnum ice cream",
                image_url="https://www.tiktok.com/api/img/?itemId=123",
                product_url="https://example.com/magnum",
                source="fake",
                search_method="retailer",
            ),
        )

    async def close(self) -> None:
        return None


class FakeResolvedProductResolutionServiceMissingTitle:
    enabled = True

    async def resolve(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ProductResolutionResult:
        return ProductResolutionResult(
            status="resolved",
            brand="Magnum",
            product_query="גלידת מגנום",
            raw_user_text=message_text,
            selected_result=SelectedProductResult(
                title="",
                description="Magnum ice cream",
                image_url="https://example.com/magnum.png",
                product_url="https://example.com/magnum",
                source="fake",
                search_method="retailer",
            ),
        )

    async def close(self) -> None:
        return None


class FakeExternalPhotoIngestor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def ingest_whatsapp_image(self, *, media_id: str) -> IngestedOperatorPhoto:
        raise RuntimeError("not used in this test")

    async def ingest_external_image_url(self, *, image_url: str) -> IngestedOperatorPhoto:
        self.calls.append(image_url)
        return IngestedOperatorPhoto(
            public_url="https://storage.example/rehosted/magnum.jpg",
            object_name="rehosted/magnum.jpg",
            content_type="image/jpeg",
            content=b"fake-image",
        )

    async def close(self) -> None:
        return None


class FakeGenerationService:
    enabled = True

    def __init__(
        self,
        *,
        poll_result: GenerationPollResult,
    ) -> None:
        self.calls = 0
        self.last_mode: GenerationMode | None = None
        self.last_draft: GenerationDraftInput | None = None
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
        variant_slot: int | None = None,
    ) -> GenerationSubmission:
        self.calls += 1
        self.last_mode = mode
        self.last_draft = draft
        slot_suffix = f"-slot{variant_slot}" if variant_slot is not None else ""
        return GenerationSubmission(
            job_id=f"job-123{slot_suffix}",
            idempotency_key=f"fixed-key{slot_suffix}",
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
        self.wait_calls += 1
        return self._poll_result

    async def close(self) -> None:
        return None


class FakeMediaStore:
    def __init__(self) -> None:
        self.upload_calls: list[tuple[bytes, str, str | None]] = []

    async def upload_bytes(
        self,
        *,
        content: bytes,
        content_type: str,
        object_name: str | None = None,
        suffix: str | None = None,
    ) -> MediaUpload:
        self.upload_calls.append((content, content_type, suffix))
        return MediaUpload(
            object_name=object_name or "generated/preview.png",
            public_url="https://storage.example/generated/preview.png",
        )

    async def has_delete_lifecycle_rule(self, *, days: int) -> bool:
        return False

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
        await OperatorRepository(session).create(
            phone=phone,
            language=language,
            active=True,
            business_name="Test Biz",
            logo_url="https://example.com/logo.png",
        )


def test_derive_aspect_ratio_for_1920x1080() -> None:
    assert derive_aspect_ratio(width=1920, height=1080) == "16:9"


def test_build_generation_prompt_marks_primary_elements_prominent() -> None:
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="גבינה לבנה",
        price=Decimal("14.90"),
        currency="ILS",
        promo_text=None,
        ean=None,
        photo_url=None,
        enriched_brand="תנובה",
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
        product_brand="מותג פרטי",
    )
    prompt = build_generation_prompt(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="make it clean",
    )

    assert "=== PRIMARY (must be large and prominent in the ad) ===" in prompt
    assert "Product Name: גבינה לבנה." in prompt
    assert "Brand: מותג פרטי." in prompt
    assert "Price: 14.90 ILS." in prompt
    assert "operator-provided brand as the final displayed brand" in prompt


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


async def test_gemini_service_generates_image_and_uploads_to_media_store() -> None:
    observed: dict[str, object] = {}
    encoded_png = base64.b64encode(b"png-binary").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["query"] = dict(request.url.params)
        observed["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            status_code=200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"text": "Generated image"},
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": encoded_png,
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        )

    media_store = FakeMediaStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeminiFlashImageAdGenerationService(
        api_key="gemini-test-key",
        model="gemini-3.1-flash-image-preview",
        media_store=media_store,
        client=client,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean=None,
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
    )

    submission = await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad image",
        wamid="wamid-gemini-1",
        width=1920,
        height=1080,
    )
    result = await service.wait_for_completion(job_id=submission.job_id)

    assert observed["path"] == "/v1beta/models/gemini-3.1-flash-image-preview:generateContent"
    query = observed["query"]
    assert isinstance(query, dict)
    assert query["key"] == "gemini-test-key"
    body = observed["body"]
    assert isinstance(body, dict)
    assert body["generationConfig"]["responseModalities"] == ["IMAGE"]
    assert media_store.upload_calls[0][0] == b"png-binary"
    assert media_store.upload_calls[0][1] == "image/png"
    assert media_store.upload_calls[0][2] == ".png"
    assert result.status == NanoBananaJobStatus.COMPLETED
    assert result.output_image_url == "https://storage.example/generated/preview.png"
    await client.aclose()


async def test_gemini_service_submit_and_poll_variant_pair() -> None:
    observed: dict[str, object] = {"prompts": [], "post_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method != "POST":
            return httpx.Response(status_code=404)
        observed["post_calls"] = int(observed["post_calls"]) + 1
        body = json.loads(request.content.decode("utf-8"))
        parts = body["contents"][0]["parts"]
        observed["prompts"].append(parts[0]["text"])
        encoded_png = base64.b64encode(f"png-{observed['post_calls']}".encode()).decode("ascii")
        return httpx.Response(
            status_code=200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": encoded_png,
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    media_store = FakeMediaStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeminiFlashImageAdGenerationService(
        api_key="gemini-test-key",
        model="gemini-3.1-flash-image-preview",
        media_store=media_store,
        client=client,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean=None,
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
    )

    submissions = await service.submit_variant_pair(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad image",
        wamid="wamid-gemini-pair-1",
        width=1920,
        height=1080,
    )
    results = await service.poll_variant_pair(submissions=submissions)

    assert len(submissions) == 2
    assert submissions[0].idempotency_key != submissions[1].idempotency_key
    assert len(results) == 2
    assert all(result.status == NanoBananaJobStatus.COMPLETED for result in results)
    assert int(observed["post_calls"]) == 2
    assert len(media_store.upload_calls) == 2
    prompts = observed["prompts"]
    assert any("Style A" in prompt for prompt in prompts)
    assert any("Style B" in prompt for prompt in prompts)
    await client.aclose()


async def test_gemini_service_includes_logo_inline_image_when_available() -> None:
    observed: dict[str, object] = {"get_urls": []}
    encoded_png = base64.b64encode(b"png-binary").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            get_urls = observed["get_urls"]
            assert isinstance(get_urls, list)
            get_urls.append(str(request.url))
            if request.url.path.endswith("/product.jpg"):
                return httpx.Response(
                    status_code=200,
                    content=b"product-bytes",
                    headers={"content-type": "image/jpeg"},
                )
            if request.url.path.endswith("/logo.png"):
                return httpx.Response(
                    status_code=200,
                    content=b"logo-bytes",
                    headers={"content-type": "image/png"},
                )
            return httpx.Response(status_code=404)

        observed["path"] = request.url.path
        observed["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            status_code=200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": encoded_png,
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        )

    media_store = FakeMediaStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeminiFlashImageAdGenerationService(
        api_key="gemini-test-key",
        model="gemini-3.1-flash-image-preview",
        media_store=media_store,
        client=client,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean=None,
        photo_url="https://assets.example/product.jpg",
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
        logo_url="https://assets.example/logo.png",
    )

    submission = await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad image",
        wamid="wamid-gemini-logo-1",
        width=1920,
        height=1080,
    )
    result = await service.wait_for_completion(job_id=submission.job_id)

    assert observed["path"] == "/v1beta/models/gemini-3.1-flash-image-preview:generateContent"
    get_urls = observed["get_urls"]
    assert isinstance(get_urls, list)
    assert "https://assets.example/product.jpg" in get_urls
    assert "https://assets.example/logo.png" in get_urls
    body = observed["body"]
    assert isinstance(body, dict)
    parts = body["contents"][0]["parts"]
    inline_parts = [part["inline_data"] for part in parts if "inline_data" in part]
    assert len(inline_parts) == 2
    assert inline_parts[0]["mime_type"] == "image/jpeg"
    assert inline_parts[1]["mime_type"] == "image/png"
    assert result.status == NanoBananaJobStatus.COMPLETED
    assert result.output_image_url == "https://storage.example/generated/preview.png"
    await client.aclose()


async def test_gemini_service_emits_trace_event_when_enabled() -> None:
    encoded_png = base64.b64encode(b"png-binary").decode("ascii")

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": encoded_png,
                                    }
                                },
                            ]
                        }
                    }
                ]
            },
        )

    media_store = FakeMediaStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeminiFlashImageAdGenerationService(
        api_key="gemini-test-key",
        model="gemini-3.1-flash-image-preview",
        media_store=media_store,
        trace_enabled=True,
        trace_max_chars=2000,
        client=client,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean=None,
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
    )
    trace_events: list[tuple[str, str | None, dict[str, object]]] = []

    async def trace_sink(
        action: str,
        operator_phone: str | None,
        metadata: dict[str, object],
    ) -> None:
        trace_events.append((action, operator_phone, metadata))

    service.set_trace_context(
        operator_phone=draft.operator_phone,
        wamid="wamid-gemini-trace-1",
        trace_sink=trace_sink,
    )
    await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad image",
        wamid="wamid-gemini-trace-1",
        width=1920,
        height=1080,
    )
    service.clear_trace_context()

    assert len(trace_events) == 1
    action, operator_phone, metadata = trace_events[0]
    assert action == "gemini_request_debug"
    assert operator_phone == draft.operator_phone
    assert metadata["provider"] == "gemini"
    assert metadata["wamid"] == "wamid-gemini-trace-1"
    payload = metadata["payload"]
    assert isinstance(payload, dict)
    assert payload["generationConfig"] == {"responseModalities": ["IMAGE"]}
    await client.aclose()


async def test_gemini_service_retries_retryable_http_error_then_succeeds() -> None:
    observed_calls = {"count": 0}
    encoded_png = base64.b64encode(b"png-binary").decode("ascii")

    def handler(_: httpx.Request) -> httpx.Response:
        observed_calls["count"] += 1
        if observed_calls["count"] == 1:
            return httpx.Response(
                status_code=500,
                json={"error": {"status": "INTERNAL", "message": "Internal error encountered."}},
            )
        return httpx.Response(
            status_code=200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": encoded_png,
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    media_store = FakeMediaStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeminiFlashImageAdGenerationService(
        api_key="gemini-test-key",
        media_store=media_store,
        client=client,
        max_submit_attempts=2,
        retry_base_seconds=0.0,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean=None,
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
    )

    submission = await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad image",
        wamid="wamid-gemini-retry-500",
        width=1920,
        height=1080,
    )
    result = await service.wait_for_completion(job_id=submission.job_id)

    assert observed_calls["count"] == 2
    assert result.status == NanoBananaJobStatus.COMPLETED
    assert result.output_image_url == "https://storage.example/generated/preview.png"
    await client.aclose()


async def test_gemini_service_retries_timeout_then_succeeds() -> None:
    observed_calls = {"count": 0}
    encoded_png = base64.b64encode(b"png-binary").decode("ascii")

    def handler(request: httpx.Request) -> httpx.Response:
        observed_calls["count"] += 1
        if observed_calls["count"] == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(
            status_code=200,
            json={
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "inlineData": {
                                        "mimeType": "image/png",
                                        "data": encoded_png,
                                    }
                                }
                            ]
                        }
                    }
                ]
            },
        )

    media_store = FakeMediaStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeminiFlashImageAdGenerationService(
        api_key="gemini-test-key",
        media_store=media_store,
        client=client,
        max_submit_attempts=2,
        retry_base_seconds=0.0,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean=None,
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
    )

    submission = await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad image",
        wamid="wamid-gemini-retry-timeout",
        width=1920,
        height=1080,
    )
    result = await service.wait_for_completion(job_id=submission.job_id)

    assert observed_calls["count"] == 2
    assert result.status == NanoBananaJobStatus.COMPLETED
    assert result.output_image_url == "https://storage.example/generated/preview.png"
    await client.aclose()


async def test_gemini_service_downloads_file_data_output_when_inline_missing() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "generativelanguage.googleapis.com":
            observed["generate_path"] = request.url.path
            return httpx.Response(
                status_code=200,
                json={
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {
                                        "fileData": {
                                            "mimeType": "image/png",
                                            "fileUri": "https://files.example/generated.png",
                                        }
                                    }
                                ]
                            }
                        }
                    ]
                },
            )
        if request.url.host == "files.example":
            observed["file_path"] = request.url.path
            return httpx.Response(
                status_code=200,
                content=b"png-from-file-uri",
                headers={"content-type": "image/png"},
            )
        return httpx.Response(status_code=404)

    media_store = FakeMediaStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeminiFlashImageAdGenerationService(
        api_key="gemini-test-key",
        media_store=media_store,
        client=client,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean=None,
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
    )

    submission = await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad image",
        wamid="wamid-gemini-file-data-1",
        width=1920,
        height=1080,
    )
    result = await service.wait_for_completion(job_id=submission.job_id)

    assert (
        observed["generate_path"] == "/v1beta/models/gemini-3.1-flash-image-preview:generateContent"
    )
    assert observed["file_path"] == "/generated.png"
    assert media_store.upload_calls[0][0] == b"png-from-file-uri"
    assert media_store.upload_calls[0][1] == "image/png"
    assert media_store.upload_calls[0][2] == ".png"
    assert result.status == NanoBananaJobStatus.COMPLETED
    await client.aclose()


async def test_gemini_service_non_image_response_includes_diagnostics() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status_code=200,
            json={
                "promptFeedback": {
                    "blockReason": "SAFETY",
                    "blockReasonMessage": "Blocked by policy checks",
                },
                "candidates": [
                    {
                        "finishReason": "SAFETY",
                        "content": {
                            "parts": [{"text": "I can only return text for this request."}]
                        },
                    }
                ],
            },
        )

    media_store = FakeMediaStore()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = GeminiFlashImageAdGenerationService(
        api_key="gemini-test-key",
        media_store=media_store,
        client=client,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean=None,
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
    )

    with pytest.raises(AdGenerationError) as exc_info:
        await service.submit_for_draft(
            draft=draft,
            mode=GenerationMode.FRESH,
            instruction_text="generate ad image",
            wamid="wamid-gemini-no-image",
            width=1920,
            height=1080,
        )

    error_text = str(exc_info.value)
    assert "did not include an image output" in error_text
    assert "block_reason=SAFETY" in error_text
    assert "finish_reason=SAFETY" in error_text
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


async def test_nano_banana_service_accepts_legacy_submit_response_shape() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json={"job_id": "legacy-job-1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = NanoBananaAdGenerationService(
        api_key="test-key",
        api_url="https://nano.example/api/v1/nanobanana/generate-2",
        status_api_url_template="https://nano.example/api/v1/nanobanana/jobs/{job_id}",
        client=client,
    )
    draft = GenerationDraftInput(
        draft_id=uuid.uuid4(),
        operator_phone="+972526508861",
        language="he",
        product_name="קוטג",
        price=Decimal("19.90"),
        currency="ILS",
        promo_text="מבצע",
        ean="7290004127326",
        photo_url=None,
        enriched_brand=None,
        enriched_category=None,
        enriched_description=None,
        preview_reference_url=None,
        rendered_image_url=None,
    )

    submission = await service.submit_for_draft(
        draft=draft,
        mode=GenerationMode.FRESH,
        instruction_text="generate ad",
        wamid="wamid-legacy-submit",
        width=1920,
        height=1080,
    )

    assert submission.job_id == "legacy-job-1"
    await client.aclose()


async def test_nano_banana_service_accepts_legacy_poll_status_shape() -> None:
    state = {"status_calls": 0}

    def handler(_: httpx.Request) -> httpx.Response:
        state["status_calls"] += 1
        if state["status_calls"] < 2:
            return httpx.Response(status_code=200, json={"status": "running"})
        return httpx.Response(
            status_code=200,
            json={
                "status": "completed",
                "output_image_url": "https://storage.googleapis.com/media/legacy-preview.png",
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = NanoBananaAdGenerationService(
        api_key="test-key",
        api_url="https://nano.example/api/v1/nanobanana/generate-2",
        status_api_url_template="https://nano.example/api/v1/nanobanana/jobs/{job_id}",
        poll_initial_seconds=0.01,
        poll_max_seconds=0.02,
        poll_timeout_seconds=1,
        client=client,
    )

    result = await service.wait_for_completion(job_id="legacy-job-2")

    assert result.status == NanoBananaJobStatus.COMPLETED
    assert result.output_image_url == "https://storage.googleapis.com/media/legacy-preview.png"
    assert state["status_calls"] == 2
    await client.aclose()


async def test_nano_banana_service_falls_back_from_jobs_to_record_info() -> None:
    state = {"jobs_calls": 0, "record_info_calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/jobs/fallback-job-1"):
            state["jobs_calls"] += 1
            return httpx.Response(
                status_code=404,
                json={
                    "status": 404,
                    "error": "Not Found",
                    "path": "/api/v1/nanobanana/jobs/fallback-job-1",
                },
            )
        if request.url.path.endswith("/record-info"):
            state["record_info_calls"] += 1
            return httpx.Response(
                status_code=200,
                json={
                    "successFlag": 1,
                    "response": {
                        "resultImageUrl": "https://storage.googleapis.com/media/fallback.png"
                    },
                },
            )
        return httpx.Response(status_code=404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = NanoBananaAdGenerationService(
        api_key="test-key",
        api_url="https://nano.example/api/v1/nanobanana/generate-2",
        status_api_url_template="https://nano.example/api/v1/nanobanana/jobs/{job_id}",
        poll_initial_seconds=0.01,
        poll_max_seconds=0.02,
        poll_timeout_seconds=1,
        client=client,
    )

    result = await service.wait_for_completion(job_id="fallback-job-1")

    assert result.status == NanoBananaJobStatus.COMPLETED
    assert result.output_image_url == "https://storage.googleapis.com/media/fallback.png"
    assert state["jobs_calls"] == 1
    assert state["record_info_calls"] == 1
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
    assert "http" not in result.reply_text.lower()
    assert result.generated_image_url == "https://storage.googleapis.com/media/preview-1.png"
    assert fake_gateway.reply_calls == 0
    assert fake_generation.calls == 2  # two variant slots
    assert fake_generation.wait_calls == 2  # polled both variants

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.PREVIEW_READY
        assert draft.generation_job_id == "job-123-slot1"
        assert draft.rendered_image_url == "https://storage.googleapis.com/media/preview-1.png"


async def test_pipeline_requests_product_confirmation_before_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000705"
    await _seed_operator(session_factory, phone, language="he")
    fake_gateway = FakeGateway()
    fake_generation = FakeGenerationService(
        poll_result=GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://storage.googleapis.com/media/preview-confirmation.png",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=fake_gateway,
        ad_generation_service=fake_generation,
        product_resolution_service=FakeResolvedProductResolutionService(),
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-product-confirm-request",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "אני רוצה לעשות מודעה לגלידת מגנום"}},
        )
    )

    assert result.status == "processed"
    assert result.deterministic_action == "product_confirmation_requested"
    assert result.generated_image_url == "https://example.com/magnum.png"
    assert result.action_buttons_prompt is not None
    assert result.action_buttons == [
        (BUTTON_CONFIRM_PRODUCT_SELECTION, "כן, זה המוצר"),
        (BUTTON_REJECT_PRODUCT_SELECTION, "לא, מוצר אחר"),
    ]
    assert result.publish_buttons_prompt is None
    assert fake_generation.calls == 0
    assert fake_generation.wait_calls == 0

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.DRAFT
        assert draft.photo_url == "https://example.com/magnum.png"
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


async def test_pipeline_product_confirmation_rehosts_unsafe_image_url(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000726"
    await _seed_operator(session_factory, phone, language="he")
    fake_ingestor = FakeExternalPhotoIngestor()
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        ad_generation_service=FakeGenerationService(
            poll_result=GenerationPollResult(
                status=NanoBananaJobStatus.COMPLETED,
                output_image_url="https://storage.googleapis.com/media/preview-unsafe.png",
            )
        ),
        product_resolution_service=FakeResolvedProductResolutionServiceUnsafeImage(),
        operator_photo_ingestor=fake_ingestor,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-product-confirm-unsafe-image",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "מודעה לגלידת מגנום"}},
        )
    )

    assert result.status == "processed"
    assert result.deterministic_action == "product_confirmation_requested"
    assert result.generated_image_url == "https://storage.example/rehosted/magnum.jpg"
    assert fake_ingestor.calls == ["https://www.tiktok.com/api/img/?itemId=123"]

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.photo_url == "https://storage.example/rehosted/magnum.jpg"


async def test_pipeline_product_confirmation_button_approves_and_generates(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000706"
    await _seed_operator(session_factory, phone, language="he")
    fake_gateway = FakeGateway()
    fake_generation = FakeGenerationService(
        poll_result=GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://storage.googleapis.com/media/preview-after-approve.png",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=fake_gateway,
        ad_generation_service=fake_generation,
        product_resolution_service=FakeResolvedProductResolutionService(),
    )

    first_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-product-confirm-approve-step1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "אני רוצה לעשות מודעה לגלידת מגנום"}},
        )
    )
    assert first_result.deterministic_action == "product_confirmation_requested"
    assert fake_generation.calls == 0

    approve_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-product-confirm-approve-step2",
            operator_phone=phone,
            raw_message={
                "type": "interactive",
                "interactive": {
                    "button_reply": {
                        "id": BUTTON_CONFIRM_PRODUCT_SELECTION,
                    }
                },
            },
        )
    )

    assert approve_result.status == "processed"
    assert approve_result.deterministic_action == "generation_completed"
    assert approve_result.generated_image_url == (
        "https://storage.googleapis.com/media/preview-after-approve.png"
    )
    assert fake_generation.calls == 2  # two variant slots
    assert fake_generation.wait_calls == 2  # polled both variants

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.PREVIEW_READY
        assert draft.awaiting_product_confirmation is False

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
        assert session_obj.pending_question_type == PendingQuestionType.VARIANT_SELECTION


async def test_pipeline_product_confirmation_button_rejects_and_clears_photo(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000707"
    await _seed_operator(session_factory, phone, language="he")
    fake_gateway = FakeGateway()
    fake_generation = FakeGenerationService(
        poll_result=GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://storage.googleapis.com/media/preview-after-reject.png",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=fake_gateway,
        ad_generation_service=fake_generation,
        product_resolution_service=FakeResolvedProductResolutionService(),
    )

    first_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-product-confirm-reject-step1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "אני רוצה לעשות מודעה לגלידת מגנום"}},
        )
    )
    assert first_result.deterministic_action == "product_confirmation_requested"
    assert fake_generation.calls == 0

    reject_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-product-confirm-reject-step2",
            operator_phone=phone,
            raw_message={
                "type": "interactive",
                "interactive": {
                    "button_reply": {
                        "id": BUTTON_REJECT_PRODUCT_SELECTION,
                    }
                },
            },
        )
    )

    assert reject_result.status == "processed"
    assert reject_result.deterministic_action == "product_confirmation_rejected"
    assert reject_result.generated_image_url is None
    assert reject_result.reply_text is not None
    assert "לא אשתמש" in reject_result.reply_text
    assert fake_generation.calls == 0
    assert fake_generation.wait_calls == 0

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.DRAFT
        assert draft.awaiting_product_confirmation is False
        assert draft.photo_url is None
        assert draft.enriched_image_url is None

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
        assert session_obj.pending_question_type == PendingQuestionType.NONE


async def test_pipeline_product_confirmation_approved_without_product_name_prompts_missing_info(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000731"
    await _seed_operator(session_factory, phone, language="he")
    fake_generation = FakeGenerationService(
        poll_result=GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://storage.googleapis.com/media/preview-after-approve.png",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGatewayNoProductName(),
        ad_generation_service=fake_generation,
        product_resolution_service=FakeResolvedProductResolutionServiceMissingTitle(),
    )

    first_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-product-confirm-no-name-step1",
            operator_phone=phone,
            raw_message={
                "type": "text",
                "text": {"body": "אני רוצה מודעה למוצר אחד גלידת מגנום"},
            },
        )
    )
    assert first_result.deterministic_action == "product_confirmation_requested"
    assert fake_generation.calls == 0

    approve_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-product-confirm-no-name-step2",
            operator_phone=phone,
            raw_message={
                "type": "interactive",
                "interactive": {
                    "button_reply": {
                        "id": BUTTON_CONFIRM_PRODUCT_SELECTION,
                    }
                },
            },
        )
    )

    assert approve_result.status == "processed"
    assert approve_result.deterministic_action == "generation_gate_blocked"
    assert approve_result.generated_image_url is None
    assert approve_result.reply_text is not None
    assert "שם המוצר" in approve_result.reply_text
    assert fake_generation.calls == 0
    assert fake_generation.wait_calls == 0

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.awaiting_product_confirmation is False
        assert draft.product_name is None

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
        assert session_obj.pending_question_type == PendingQuestionType.MISSING_INFO
        assert session_obj.pending_question_context["question_key"] == "product_name"
        assert session_obj.pending_question_context["required"] is True


async def test_pipeline_brand_conflict_uses_operator_brand_and_logs_audit(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000704"
    await _seed_operator(session_factory, phone)
    fake_gateway = FakeGatewayBrandConflict()
    fake_generation = FakeGenerationService(
        poll_result=GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://storage.googleapis.com/media/preview-brand-conflict.png",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=fake_gateway,
        enrichment_service=StaticEnrichmentService(
            EnrichedProduct(
                product_name="White Cheese",
                brand="Tnuva",
                source="open_food_facts",
            )
        ),
        ad_generation_service=fake_generation,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-brand-conflict",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad for white cheese 14.90"}},
        )
    )

    assert result.status == "processed"
    assert result.deterministic_action == "generation_completed"
    assert result.reply_text is not None
    assert "you wrote" in result.reply_text.lower()
    assert fake_generation.calls == 2  # two variant slots
    assert fake_generation.last_draft is not None
    assert fake_generation.last_draft.product_brand == "Private Label"
    assert fake_generation.last_draft.enriched_brand == "Tnuva"

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.product_brand == "Private Label"
        assert draft.enriched_brand == "Tnuva"

        conflict_events = (
            await session.execute(
                select(AuditEvent).where(
                    AuditEvent.operator_phone == phone,
                    AuditEvent.action == "product_brand_conflict_detected",
                )
            )
        ).scalars()
        assert len(list(conflict_events)) == 1


async def test_pipeline_generates_preview_without_price_and_requests_followup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000703"
    await _seed_operator(session_factory, phone)
    fake_gateway = FakeGatewayNoPrice()
    fake_generation = FakeGenerationService(
        poll_result=GenerationPollResult(
            status=NanoBananaJobStatus.COMPLETED,
            output_image_url="https://storage.googleapis.com/media/preview-no-price.png",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=fake_gateway,
        ad_generation_service=fake_generation,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase7-generate-no-price",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad for cottage"}},
        )
    )

    assert result.status == "processed"
    assert result.deterministic_action == "generation_completed"
    assert result.generated_image_url == "https://storage.googleapis.com/media/preview-no-price.png"
    assert result.reply_text is not None
    assert result.action_buttons_prompt is not None  # variant selection shown
    assert result.action_buttons is not None
    assert result.publish_buttons_prompt is None  # deferred until after variant selection
    assert fake_generation.calls == 2  # two variant slots
    assert fake_generation.last_draft is not None
    assert fake_generation.last_draft.price is None

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.PREVIEW_READY
        assert draft.price is None


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
    assert result.generated_image_url is None

    async with session_scope(session_factory) as session:
        draft = (
            (await session.execute(select(AdDraft).where(AdDraft.operator_phone == phone)))
            .scalars()
            .first()
        )
        assert draft is not None
        assert draft.status == AdDraftStatus.DRAFT
