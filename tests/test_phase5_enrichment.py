from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.base import Base
from adv_assistant.db.models import AdDraft, AuditEvent, ConversationSession
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.enrichment import (
    BarcodeDecoderChain,
    EnrichedProduct,
    NoopProductLookupProvider,
    OpenFoodFactsProvider,
    ProviderChainEnrichmentService,
)
from adv_assistant.llm_gateway import (
    ExtractedAdFields,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


class StaticBarcodeDecoder:
    def __init__(self, value: str | None) -> None:
        self._value = value
        self.calls = 0

    async def decode_ean(self, image_bytes: bytes) -> str | None:
        self.calls += 1
        return self._value


class StaticProvider:
    def __init__(self, source: str, result: EnrichedProduct | None) -> None:
        self._source = source
        self._result = result
        self.calls = 0

    @property
    def source(self) -> str:
        return self._source

    async def lookup(self, *, ean: str, language: str) -> EnrichedProduct | None:
        self.calls += 1
        return self._result

    async def close(self) -> None:
        return None


class FakeGateway:
    uses_external_llm = False

    async def classify_intent(
        self, *, message_text: str, language: str, history: list[dict[str, str]]
    ) -> IntentClassification:
        return IntentClassification(intent=Intent.CREATE_AD)

    async def extract_ad_fields(
        self, *, message_text: str, language: str, history: list[dict[str, str]]
    ) -> ExtractedAdFields:
        return ExtractedAdFields(ean="7290001234567")

    async def generate_reply(
        self,
        *,
        intent: Intent,
        message_text: str,
        language: str,
        extracted_fields: ExtractedAdFields | None,
    ) -> ReplyGeneration:
        return ReplyGeneration(reply_text="Draft updated.")


class StaticEnrichmentService:
    def __init__(self, result: EnrichedProduct | None) -> None:
        self._result = result
        self.calls = 0

    async def decode_ean_from_image(self, image_bytes: bytes) -> str | None:
        return None

    async def enrich_by_ean(self, *, ean: str, language: str) -> EnrichedProduct | None:
        self.calls += 1
        return self._result

    async def close(self) -> None:
        return None


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "phase5.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_operator(factory: async_sessionmaker[AsyncSession], phone: str) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(phone=phone, active=True)


async def test_open_food_facts_provider_returns_normalized_schema() -> None:
    payload = {
        "status": 1,
        "product": {
            "product_name_he": "קוקה קולה",
            "brands": "Coca-Cola, The Coca-Cola Company",
            "categories": "Beverages,Soft drinks",
            "generic_name_he": "משקה קל מוגז",
            "image_front_url": "https://images.example/coke-front.jpg",
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/product/7290001234567"
        return httpx.Response(status_code=200, json=payload)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://world.openfoodfacts.org",
    )
    provider = OpenFoodFactsProvider(client=client)
    result = await provider.lookup(ean="7290001234567", language="he")

    assert result is not None
    assert result.product_name == "קוקה קולה"
    assert result.brand == "Coca-Cola"
    assert result.category == "Beverages,Soft drinks"
    assert result.description == "משקה קל מוגז"
    assert result.image_url == "https://images.example/coke-front.jpg"
    assert result.source == "open_food_facts"
    await client.aclose()


async def test_provider_chain_uses_fallback_order() -> None:
    provider_1 = StaticProvider("open_food_facts", None)
    provider_2 = StaticProvider(
        "ean_fallback",
        EnrichedProduct(product_name="Fallback Name", source="ean_fallback"),
    )
    provider_3 = StaticProvider("web_search", EnrichedProduct(product_name="web", source="web"))
    service = ProviderChainEnrichmentService(providers=[provider_1, provider_2, provider_3])

    result = await service.enrich_by_ean(ean="7290001234567", language="he")

    assert result is not None
    assert result.source == "ean_fallback"
    assert provider_1.calls == 1
    assert provider_2.calls == 1
    assert provider_3.calls == 0


async def test_barcode_decoder_chain_uses_vision_fallback() -> None:
    primary = StaticBarcodeDecoder(None)
    fallback = StaticBarcodeDecoder("7290001234567")
    chain = BarcodeDecoderChain(primary=primary, fallback=fallback)

    result = await chain.decode_ean(b"image-bytes")

    assert result == "7290001234567"
    assert primary.calls == 1
    assert fallback.calls == 1


async def test_pipeline_applies_normalized_enrichment_only(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000501"
    await _seed_operator(session_factory, phone)
    enrichment_service = StaticEnrichmentService(
        EnrichedProduct(
            product_name="קפה שחור",
            brand="Elite",
            category="Coffee",
            description="אריזה משפחתית",
            image_url="https://example.com/coffee.png",
            source="open_food_facts",
        )
    )
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        enrichment_service=enrichment_service,
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-enrich-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad 7290001234567"}},
        )
    )

    assert result.status == "processed"
    assert enrichment_service.calls == 1

    async with session_scope(session_factory) as session:
        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.product_name == "קפה שחור"
        assert draft.promo_text == "אריזה משפחתית"
        assert draft.photo_url == "https://example.com/coffee.png"
        assert draft.enriched_brand == "Elite"
        assert draft.enriched_category == "Coffee"
        assert draft.enriched_description == "אריזה משפחתית"
        assert draft.enriched_image_url == "https://example.com/coffee.png"
        assert draft.enrichment_source == "open_food_facts"

        audit_rows = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "enrichment_applied")
            )
        ).scalars()
        audit_event = list(audit_rows)[0]
        assert "updated_fields" in audit_event.metadata_json
        assert "raw_payload" not in audit_event.metadata_json


async def test_enrichment_unavailable_notified_once_per_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000502"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=FakeGateway(),
        enrichment_service=StaticEnrichmentService(None),
    )

    first = await processor.process(
        InboundTaskPayload(
            wamid="wamid-enrich-miss-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad 7290001234567"}},
        )
    )
    second = await processor.process(
        InboundTaskPayload(
            wamid="wamid-enrich-miss-2",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "create ad 7290001234567"}},
        )
    )

    assert first.reply_text is not None
    assert "could not find additional product details" in first.reply_text.lower()
    assert second.reply_text is not None
    assert "could not find additional product details" not in second.reply_text.lower()

    async with session_scope(session_factory) as session:
        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.enrichment_unavailable_notified_at is not None


def test_no_raw_enrichment_payload_field_is_defined() -> None:
    assert not hasattr(AdDraft, "enrichment_raw_payload")
    assert isinstance(NoopProductLookupProvider("none"), NoopProductLookupProvider)
