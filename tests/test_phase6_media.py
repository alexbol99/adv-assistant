from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.base import Base
from adv_assistant.db.models import AdDraft, AuditEvent, ConversationSession, Operator
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.enrichment import EnrichedProduct
from adv_assistant.llm_gateway import (
    ExtractedAdFields,
    Intent,
    IntentClassification,
    ReplyGeneration,
)
from adv_assistant.media_ingest import (
    DownloadedMedia,
    IngestedOperatorPhoto,
    MediaIngestError,
    OperatorPhotoIngestor,
)
from adv_assistant.media_store import GCSMediaStore, MediaStore, MediaUpload, S3MediaStore
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


class FakeStorageBlob:
    def __init__(self, bucket: "FakeStorageBucket", name: str) -> None:
        self._bucket = bucket
        self._name = name

    def upload_from_string(self, content: bytes, content_type: str) -> None:
        self._bucket.uploads[self._name] = (content, content_type)


class FakeStorageBucket:
    def __init__(self, name: str) -> None:
        self.name = name
        self.uploads: dict[str, tuple[bytes, str]] = {}
        self.lifecycle_rules: list[dict[str, object]] = []
        self.reloaded = False

    def blob(self, name: str) -> FakeStorageBlob:
        return FakeStorageBlob(self, name)

    def reload(self) -> None:
        self.reloaded = True


class FakeStorageClient:
    def __init__(self) -> None:
        self._buckets: dict[str, FakeStorageBucket] = {}

    def bucket(self, name: str) -> FakeStorageBucket:
        if name not in self._buckets:
            self._buckets[name] = FakeStorageBucket(name)
        return self._buckets[name]


class FakeS3LifecycleError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3Client:
    def __init__(self) -> None:
        self.uploads: dict[tuple[str, str], tuple[bytes, str]] = {}
        self.lifecycle_rules: list[dict[str, object]] | None = []

    def put_object(
        self,
        *,
        Bucket: str,
        Key: str,
        Body: bytes,
        ContentType: str,
    ) -> None:
        self.uploads[(Bucket, Key)] = (Body, ContentType)

    def get_bucket_lifecycle_configuration(self, *, Bucket: str) -> dict[str, object]:
        if self.lifecycle_rules is None:
            raise FakeS3LifecycleError("NoSuchLifecycleConfiguration")
        return {"Rules": self.lifecycle_rules}


class StaticMediaStore(MediaStore):
    def __init__(self, public_url: str) -> None:
        self.public_url = public_url
        self.last_upload: tuple[bytes, str, str | None] | None = None

    async def upload_bytes(
        self,
        *,
        content: bytes,
        content_type: str,
        object_name: str | None = None,
        suffix: str | None = None,
    ) -> MediaUpload:
        self.last_upload = (content, content_type, suffix)
        return MediaUpload(
            object_name=object_name or f"operator-photos/mock{suffix or ''}",
            public_url=self.public_url,
        )

    async def has_delete_lifecycle_rule(self, *, days: int) -> bool:
        return True

    async def close(self) -> None:
        return None


class StaticWhatsAppMediaClient:
    def __init__(self, downloaded: DownloadedMedia) -> None:
        self.downloaded = downloaded
        self.calls = 0

    async def download_media(self, *, media_id: str) -> DownloadedMedia:
        self.calls += 1
        return self.downloaded

    async def close(self) -> None:
        return None


class StaticPhotoIngestor(OperatorPhotoIngestor):
    def __init__(
        self, result: IngestedOperatorPhoto | None = None, error: str | None = None
    ) -> None:
        self._result = result
        self._error = error

    async def ingest_whatsapp_image(self, *, media_id: str) -> IngestedOperatorPhoto:
        if self._error:
            raise MediaIngestError(self._error)
        if self._result is None:
            raise MediaIngestError("missing mock result")
        return self._result

    async def close(self) -> None:
        return None


class StaticEnrichmentService:
    def __init__(self, *, decoded_ean: str | None, enriched: EnrichedProduct | None) -> None:
        self._decoded_ean = decoded_ean
        self._enriched = enriched

    async def decode_ean_from_image(self, image_bytes: bytes) -> str | None:
        return self._decoded_ean

    async def enrich_by_ean(self, *, ean: str, language: str) -> EnrichedProduct | None:
        return self._enriched

    async def close(self) -> None:
        return None


class StaticLogoIntentGateway:
    uses_external_llm = False

    def __init__(self) -> None:
        self.classify_calls = 0

    async def classify_intent(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> IntentClassification:
        self.classify_calls += 1
        if self.classify_calls == 1:
            return IntentClassification(intent=Intent.SET_LOGO)
        return IntentClassification(intent=Intent.UNKNOWN)

    async def extract_ad_fields(
        self,
        *,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
    ) -> ExtractedAdFields:
        return ExtractedAdFields()

    async def extract_branding_fields(
        self,
        *,
        message_text: str,
        language: str,
    ):
        raise RuntimeError("not used in this test")

    async def generate_reply(
        self,
        *,
        intent: Intent,
        message_text: str,
        language: str,
        extracted_fields: ExtractedAdFields | None,
    ) -> ReplyGeneration:
        return ReplyGeneration(reply_text="fallback")


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "phase6.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


async def _seed_operator(factory: async_sessionmaker[AsyncSession], phone: str) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(phone=phone, active=True)


async def test_gcs_media_store_upload_and_lifecycle_rule_check() -> None:
    fake_client = FakeStorageClient()
    bucket = fake_client.bucket("test-media")
    bucket.lifecycle_rules = [{"action": {"type": "Delete"}, "condition": {"age": 90}}]
    store = GCSMediaStore(bucket_name="test-media", client=fake_client)  # type: ignore[arg-type]

    upload = await store.upload_bytes(content=b"img", content_type="image/png", suffix=".png")

    assert upload.object_name.startswith("operator-photos/")
    assert upload.object_name.endswith(".png")
    assert upload.public_url.startswith("https://storage.googleapis.com/test-media/")
    assert bucket.uploads[upload.object_name] == (b"img", "image/png")
    assert await store.has_delete_lifecycle_rule(days=90) is True
    assert await store.has_delete_lifecycle_rule(days=30) is False
    assert bucket.reloaded is True


async def test_s3_media_store_upload_and_lifecycle_rule_check() -> None:
    fake_client = FakeS3Client()
    fake_client.lifecycle_rules = [{"Status": "Enabled", "Expiration": {"Days": 90}}]
    store = S3MediaStore(
        bucket_name="test-media",
        region_name="eu-west-1",
        client=fake_client,
    )

    upload = await store.upload_bytes(content=b"img", content_type="image/png", suffix=".png")

    assert upload.object_name.startswith("operator-photos/")
    assert upload.object_name.endswith(".png")
    assert upload.public_url.startswith("https://test-media.s3.eu-west-1.amazonaws.com/")
    assert fake_client.uploads[("test-media", upload.object_name)] == (b"img", "image/png")
    assert await store.has_delete_lifecycle_rule(days=90) is True
    assert await store.has_delete_lifecycle_rule(days=30) is False

    fake_client.lifecycle_rules = None
    assert await store.has_delete_lifecycle_rule(days=90) is False


async def test_default_operator_photo_ingestor_uploads_image() -> None:
    media_store = StaticMediaStore(public_url="https://storage.googleapis.com/bucket/operator.jpg")
    media_client = StaticWhatsAppMediaClient(
        DownloadedMedia(content=b"image-bytes", content_type="image/jpeg")
    )
    from adv_assistant.media_ingest import DefaultOperatorPhotoIngestor

    ingestor = DefaultOperatorPhotoIngestor(media_client=media_client, media_store=media_store)
    ingested = await ingestor.ingest_whatsapp_image(media_id="abc123")

    assert ingested.public_url.endswith("/operator.jpg")
    assert ingested.content == b"image-bytes"
    assert media_store.last_upload is not None
    assert media_store.last_upload[2] == ".jpg"


async def test_default_operator_photo_ingestor_rejects_non_image() -> None:
    media_store = StaticMediaStore(public_url="https://storage.googleapis.com/bucket/object")
    media_client = StaticWhatsAppMediaClient(
        DownloadedMedia(content=b"binary", content_type="application/pdf")
    )
    from adv_assistant.media_ingest import DefaultOperatorPhotoIngestor

    ingestor = DefaultOperatorPhotoIngestor(media_client=media_client, media_store=media_store)

    with pytest.raises(MediaIngestError):
        await ingestor.ingest_whatsapp_image(media_id="abc123")


async def test_pipeline_processes_image_message_and_updates_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000601"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(
        session_factory,
        operator_photo_ingestor=StaticPhotoIngestor(
            IngestedOperatorPhoto(
                public_url="https://storage.googleapis.com/test-media/operator-photos/p1.jpg",
                object_name="operator-photos/p1.jpg",
                content_type="image/jpeg",
                content=b"jpeg-bytes",
            )
        ),
        enrichment_service=StaticEnrichmentService(
            decoded_ean="7290004127326",
            enriched=EnrichedProduct(
                product_name="קוטג תנובה",
                brand="תנובה",
                source="open_food_facts",
            ),
        ),
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase6-image-1",
            operator_phone=phone,
            raw_message={"type": "image", "image": {"id": "meta-media-1"}},
        )
    )

    assert result.status == "processed"
    assert result.deterministic_action == "operator_photo_ingest"
    assert result.reply_text is not None
    assert "7290004127326" in result.reply_text

    async with session_scope(session_factory) as session:
        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.photo_url == "https://storage.googleapis.com/test-media/operator-photos/p1.jpg"
        assert draft.ean == "7290004127326"
        assert draft.product_name == "קוטג תנובה"

        audit_rows = (
            await session.execute(
                select(AuditEvent).where(AuditEvent.action == "operator_photo_ingested")
            )
        ).scalars()
        assert len(list(audit_rows)) == 1


async def test_pipeline_image_ingest_failure_is_user_visible(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000602"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(
        session_factory,
        operator_photo_ingestor=StaticPhotoIngestor(error="meta 401"),
        enrichment_service=StaticEnrichmentService(decoded_ean=None, enriched=None),
    )

    result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase6-image-err",
            operator_phone=phone,
            raw_message={"type": "image", "image": {"id": "meta-media-err"}},
        )
    )

    assert result.status == "processed"
    assert result.reply_text is not None
    assert "could not process your photo" in result.reply_text.lower()


async def test_pipeline_set_logo_routes_next_image_to_operator_logo(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000603"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=StaticLogoIntentGateway(),
        operator_photo_ingestor=StaticPhotoIngestor(
            IngestedOperatorPhoto(
                public_url="https://storage.googleapis.com/test-media/operator-logos/l1.jpg",
                object_name="operator-logos/l1.jpg",
                content_type="image/jpeg",
                content=b"jpeg-bytes",
            )
        ),
        enrichment_service=StaticEnrichmentService(decoded_ean=None, enriched=None),
    )

    logo_prompt = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase6-logo-1",
            operator_phone=phone,
            raw_message={"type": "text", "text": {"body": "I want to set my logo"}},
        )
    )
    assert logo_prompt.status == "processed"
    assert logo_prompt.reply_text is not None
    assert "logo" in logo_prompt.reply_text.lower() or "לוגו" in logo_prompt.reply_text

    upload_result = await processor.process(
        InboundTaskPayload(
            wamid="wamid-phase6-logo-2",
            operator_phone=phone,
            raw_message={"type": "image", "image": {"id": "meta-logo-media-1"}},
        )
    )
    assert upload_result.status == "processed"
    assert upload_result.deterministic_action == "operator_logo_upload"

    async with session_scope(session_factory) as session:
        operator = (
            await session.execute(select(Operator).where(Operator.phone == phone))
        ).scalar_one()
        assert (
            operator.logo_url == "https://storage.googleapis.com/test-media/operator-logos/l1.jpg"
        )

        session_obj = (
            await session.execute(
                select(ConversationSession).where(ConversationSession.operator_phone == phone)
            )
        ).scalar_one()
        assert session_obj.pending_upload_type is None
        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.photo_url is None
