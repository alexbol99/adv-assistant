import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.cms_cityscreen import CMSPublishError, CMSPublishResult
from adv_assistant.db.base import Base
from adv_assistant.db.enums import AdDraftStatus
from adv_assistant.db.models import AdDraft, PublishedAd
from adv_assistant.db.repositories import (
    AdDraftRepository,
    ConversationSessionRepository,
    OperatorRepository,
)
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.llm_gateway import BUTTON_CONFIRM_PUBLISH
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


class FakeCMSPublisher:
    def __init__(self) -> None:
        self.enabled = True
        self.calls = 0
        self.last_image_url: str | None = None
        self.last_title: str | None = None
        self.last_campaign_id: int | None = None
        self.last_playlist_id: int | None = None
        self.target_calls: list[tuple[int | None, int | None]] = []
        self.raise_error: Exception | None = None

    async def publish_generated_image(
        self,
        *,
        image_url: str,
        title: str,
        campaign_id: int | None = None,
        playlist_id: int | None = None,
    ) -> CMSPublishResult:
        self.calls += 1
        self.last_image_url = image_url
        self.last_title = title
        self.last_campaign_id = campaign_id
        self.last_playlist_id = playlist_id
        self.target_calls.append((campaign_id, playlist_id))
        if self.raise_error is not None:
            raise self.raise_error
        return CMSPublishResult(file_id=1280, advertisement_id=975, slot_id=336)

    async def close(self) -> None:
        return None


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "phase8.db"
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
    with_mapping: bool = True,
    campaign_id: int = 157,
    playlist_id: int = 139,
) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(
            phone=phone,
            active=True,
            cms_campaign_id=campaign_id if with_mapping else None,
            cms_playlist_id=playlist_id if with_mapping else None,
        )


async def _seed_preview_draft(
    factory: async_sessionmaker[AsyncSession],
    *,
    phone: str,
    rendered_image_url: str,
) -> uuid.UUID:
    async with session_scope(factory) as session:
        draft_repo = AdDraftRepository(session)
        draft = await draft_repo.create(
            operator_phone=phone,
            status=AdDraftStatus.PREVIEW_READY,
            product_name="Milk",
            rendered_image_url=rendered_image_url,
            preview_reference_url=rendered_image_url,
        )
        await ConversationSessionRepository(session).create_or_update(
            operator_phone=phone,
            history=[],
            current_draft_id=draft.id,
        )
        return draft.id


async def test_confirm_publish_button_publishes_to_cms_and_marks_draft_published(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000801"
    await _seed_operator(session_factory, phone)
    draft_id = await _seed_preview_draft(
        session_factory,
        phone=phone,
        rendered_image_url="https://storage.googleapis.com/media/preview.png",
    )
    cms = FakeCMSPublisher()
    processor = InboundTaskProcessor(session_factory, cms_publisher=cms)

    payload = InboundTaskPayload(
        wamid="wamid-phase8-confirm-publish",
        operator_phone=phone,
        raw_message={
            "type": "interactive",
            "interactive": {"button_reply": {"id": BUTTON_CONFIRM_PUBLISH}},
        },
    )
    result = await processor.process(payload)

    assert result.status == "processed"
    assert result.deterministic_action == "confirm_publish"
    assert result.reply_text is not None
    assert (
        "הפרסום הצליח" in result.reply_text or "publishing succeeded" in result.reply_text.lower()
    )
    assert cms.calls == 1
    assert cms.last_image_url == "https://storage.googleapis.com/media/preview.png"
    assert cms.last_title == "Milk"
    assert cms.last_campaign_id == 157
    assert cms.last_playlist_id == 139

    async with session_scope(session_factory) as session:
        updated_draft = await session.get(AdDraft, draft_id)
        assert updated_draft is not None
        assert updated_draft.status == AdDraftStatus.PUBLISHED

        published = await session.execute(
            select(PublishedAd).where(PublishedAd.ad_draft_id == draft_id)
        )
        published_row = published.scalar_one_or_none()
        assert published_row is not None
        assert published_row.cms_id == "975"


async def test_confirm_publish_button_returns_error_message_when_cms_fails(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000802"
    await _seed_operator(session_factory, phone)
    await _seed_preview_draft(
        session_factory,
        phone=phone,
        rendered_image_url="https://storage.googleapis.com/media/preview.png",
    )
    cms = FakeCMSPublisher()
    cms.raise_error = CMSPublishError("cityscreen 400")
    processor = InboundTaskProcessor(session_factory, cms_publisher=cms)

    payload = InboundTaskPayload(
        wamid="wamid-phase8-confirm-publish-fail",
        operator_phone=phone,
        raw_message={
            "type": "interactive",
            "interactive": {"button_reply": {"id": BUTTON_CONFIRM_PUBLISH}},
        },
    )
    result = await processor.process(payload)

    assert result.status == "processed"
    assert result.deterministic_action == "confirm_publish"
    assert result.reply_text is not None
    assert "הפרסום נכשל" in result.reply_text or "publishing failed" in result.reply_text.lower()
    assert "cityscreen 400" in result.reply_text


async def test_confirm_publish_button_blocks_when_operator_cms_mapping_missing(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000803"
    await _seed_operator(session_factory, phone, with_mapping=False)
    draft_id = await _seed_preview_draft(
        session_factory,
        phone=phone,
        rendered_image_url="https://storage.googleapis.com/media/preview.png",
    )
    cms = FakeCMSPublisher()
    processor = InboundTaskProcessor(session_factory, cms_publisher=cms)

    payload = InboundTaskPayload(
        wamid="wamid-phase8-confirm-publish-no-mapping",
        operator_phone=phone,
        raw_message={
            "type": "interactive",
            "interactive": {"button_reply": {"id": BUTTON_CONFIRM_PUBLISH}},
        },
    )
    result = await processor.process(payload)

    assert result.status == "processed"
    assert result.deterministic_action == "confirm_publish"
    assert result.reply_text == "אתה לא מחובר למערכת כרגע, פנה לתמיכה כדי לייצר את החיבור"
    assert cms.calls == 0

    async with session_scope(session_factory) as session:
        updated_draft = await session.get(AdDraft, draft_id)
        assert updated_draft is not None
        assert updated_draft.status == AdDraftStatus.PREVIEW_READY


async def test_confirm_publish_routes_by_operator_cms_mapping(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone_a = "+972500000804"
    phone_b = "+972500000805"
    await _seed_operator(session_factory, phone_a, campaign_id=157, playlist_id=139)
    await _seed_operator(session_factory, phone_b, campaign_id=501, playlist_id=808)
    await _seed_preview_draft(
        session_factory,
        phone=phone_a,
        rendered_image_url="https://storage.googleapis.com/media/preview-a.png",
    )
    await _seed_preview_draft(
        session_factory,
        phone=phone_b,
        rendered_image_url="https://storage.googleapis.com/media/preview-b.png",
    )
    cms = FakeCMSPublisher()
    processor = InboundTaskProcessor(session_factory, cms_publisher=cms)

    payload_a = InboundTaskPayload(
        wamid="wamid-phase8-confirm-publish-route-a",
        operator_phone=phone_a,
        raw_message={
            "type": "interactive",
            "interactive": {"button_reply": {"id": BUTTON_CONFIRM_PUBLISH}},
        },
    )
    payload_b = InboundTaskPayload(
        wamid="wamid-phase8-confirm-publish-route-b",
        operator_phone=phone_b,
        raw_message={
            "type": "interactive",
            "interactive": {"button_reply": {"id": BUTTON_CONFIRM_PUBLISH}},
        },
    )

    result_a = await processor.process(payload_a)
    result_b = await processor.process(payload_b)

    assert result_a.status == "processed"
    assert result_b.status == "processed"
    assert cms.target_calls == [(157, 139), (501, 808)]
