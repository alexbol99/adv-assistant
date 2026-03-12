"""Tests for T4: first-time onboarding gate (business name + logo required)."""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.base import Base
from adv_assistant.db.enums import PendingQuestionType
from adv_assistant.db.models import AdDraft, ConversationSession, Operator
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.media_ingest import MediaIngestError
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "onboarding.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = create_session_factory(engine)
    yield factory
    await engine.dispose()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _seed_operator(
    factory: async_sessionmaker[AsyncSession],
    phone: str,
    *,
    business_name: str | None = None,
    logo_url: str | None = None,
    language: str = "he",
) -> None:
    async with session_scope(factory) as session:
        await OperatorRepository(session).create(
            phone=phone,
            active=True,
            language=language,
            business_name=business_name,
            logo_url=logo_url,
        )


async def _get_operator(
    factory: async_sessionmaker[AsyncSession], phone: str
) -> Operator:
    async with session_scope(factory) as session:
        op = await OperatorRepository(session).get_by_phone(phone)
        assert op is not None
        return op


async def _get_session(
    factory: async_sessionmaker[AsyncSession], phone: str
) -> ConversationSession | None:
    async with session_scope(factory) as session:
        result = await session.execute(
            select(ConversationSession).where(
                ConversationSession.operator_phone == phone
            )
        )
        return result.scalar_one_or_none()


async def _count_drafts(factory: async_sessionmaker[AsyncSession]) -> int:
    async with session_scope(factory) as session:
        result = await session.execute(select(AdDraft))
        return len(list(result.scalars().all()))


def _text_payload(*, wamid: str, phone: str, text: str) -> InboundTaskPayload:
    return InboundTaskPayload(
        wamid=wamid,
        operator_phone=phone,
        raw_message={"type": "text", "text": {"body": text}},
    )


def _image_payload(
    *, wamid: str, phone: str, media_id: str = "media-123"
) -> InboundTaskPayload:
    return InboundTaskPayload(
        wamid=wamid,
        operator_phone=phone,
        raw_message={"type": "image", "image": {"id": media_id}},
    )


@dataclass(slots=True)
class FakeDownloadedMedia:
    content: bytes
    content_type: str
    public_url: str


class FakePhotoIngestor:
    """Fake ingestor that returns a fixed URL."""

    def __init__(self, public_url: str = "https://cdn.example.com/logo.png") -> None:
        self._public_url = public_url

    async def ingest_whatsapp_image(self, *, media_id: str) -> FakeDownloadedMedia:
        return FakeDownloadedMedia(
            content=b"logo-bytes",
            content_type="image/png",
            public_url=self._public_url,
        )


class FailingPhotoIngestor:
    """Fake ingestor that raises MediaIngestError."""

    async def ingest_whatsapp_image(self, *, media_id: str) -> FakeDownloadedMedia:
        raise MediaIngestError("upload failed")


# ---------------------------------------------------------------------------
# Tests: Onboarding Welcome (first message, no branding)
# ---------------------------------------------------------------------------


async def test_first_message_triggers_onboarding_welcome(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001001"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(session_factory)

    result = await processor.process(
        _text_payload(wamid="wamid-ob-1", phone=phone, text="shalom")
    )

    assert result.status == "processed"
    assert result.deterministic_action == "onboarding"
    assert result.intent == "onboarding"
    assert result.llm_used is False
    assert result.draft_created is False
    assert result.reply_text is not None
    assert "שם העסק" in result.reply_text


async def test_first_message_image_triggers_welcome(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Even if the first message is an image, we show welcome and ask for name."""
    phone = "+972500001002"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(session_factory)

    result = await processor.process(
        _image_payload(wamid="wamid-ob-2", phone=phone)
    )

    assert result.deterministic_action == "onboarding"
    assert result.draft_created is False
    assert "שם העסק" in result.reply_text


# ---------------------------------------------------------------------------
# Tests: Name step
# ---------------------------------------------------------------------------


async def test_name_step_accepts_text_and_advances_to_logo(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001003"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(session_factory)

    # First message: welcome
    await processor.process(
        _text_payload(wamid="wamid-ob-3a", phone=phone, text="hello")
    )

    # Second message: business name
    result = await processor.process(
        _text_payload(wamid="wamid-ob-3b", phone=phone, text="Fresh Market")
    )

    assert result.deterministic_action == "onboarding"
    assert result.draft_created is False
    assert "Fresh Market" in result.reply_text
    assert "לוגו" in result.reply_text

    op = await _get_operator(session_factory, phone)
    assert op.business_name == "Fresh Market"
    assert op.logo_url is None


async def test_name_step_rejects_image(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001004"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(session_factory)

    # Trigger welcome first
    await processor.process(
        _text_payload(wamid="wamid-ob-4a", phone=phone, text="hi")
    )
    # Send image when name expected
    result = await processor.process(
        _image_payload(wamid="wamid-ob-4b", phone=phone)
    )

    assert result.deterministic_action == "onboarding"
    assert "טקסט" in result.reply_text

    op = await _get_operator(session_factory, phone)
    assert op.business_name is None


# ---------------------------------------------------------------------------
# Tests: Logo step
# ---------------------------------------------------------------------------


async def test_logo_step_accepts_image_and_completes(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001005"
    logo_url = "https://cdn.example.com/onboarding-logo.png"
    await _seed_operator(
        session_factory, phone, business_name="Super Market"
    )
    processor = InboundTaskProcessor(
        session_factory, operator_photo_ingestor=FakePhotoIngestor(logo_url)
    )

    result = await processor.process(
        _image_payload(wamid="wamid-ob-5", phone=phone)
    )

    assert result.deterministic_action == "onboarding"
    assert "הושלמה" in result.reply_text

    op = await _get_operator(session_factory, phone)
    assert op.logo_url == logo_url

    session_obj = await _get_session(session_factory, phone)
    assert session_obj is not None
    assert session_obj.pending_question_type == PendingQuestionType.NONE


async def test_logo_step_rejects_text(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001006"
    await _seed_operator(
        session_factory, phone, business_name="Super Market"
    )
    processor = InboundTaskProcessor(session_factory)

    result = await processor.process(
        _text_payload(wamid="wamid-ob-6", phone=phone, text="not an image")
    )

    assert result.deterministic_action == "onboarding"
    assert "לוגו" in result.reply_text
    assert "תמונה" in result.reply_text

    op = await _get_operator(session_factory, phone)
    assert op.logo_url is None


async def test_logo_upload_failure_retains_state(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001007"
    await _seed_operator(
        session_factory, phone, business_name="Super Market"
    )
    processor = InboundTaskProcessor(
        session_factory, operator_photo_ingestor=FailingPhotoIngestor()
    )

    result = await processor.process(
        _image_payload(wamid="wamid-ob-7", phone=phone)
    )

    assert result.deterministic_action == "onboarding"
    assert "לא הצלחתי" in result.reply_text

    op = await _get_operator(session_factory, phone)
    assert op.logo_url is None


# ---------------------------------------------------------------------------
# Tests: Full flow
# ---------------------------------------------------------------------------


async def test_full_onboarding_flow(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001008"
    await _seed_operator(session_factory, phone)
    logo_url = "https://cdn.example.com/full-flow-logo.png"
    processor = InboundTaskProcessor(
        session_factory, operator_photo_ingestor=FakePhotoIngestor(logo_url)
    )

    # Step 1: first message -> welcome
    r1 = await processor.process(
        _text_payload(wamid="wamid-full-1", phone=phone, text="hello")
    )
    assert r1.deterministic_action == "onboarding"
    assert "שם העסק" in r1.reply_text

    # Step 2: provide name -> ask for logo
    r2 = await processor.process(
        _text_payload(wamid="wamid-full-2", phone=phone, text="My Shop")
    )
    assert r2.deterministic_action == "onboarding"
    assert "My Shop" in r2.reply_text
    assert "לוגו" in r2.reply_text

    # Step 3: provide logo -> onboarding complete
    r3 = await processor.process(
        _image_payload(wamid="wamid-full-3", phone=phone)
    )
    assert r3.deterministic_action == "onboarding"
    assert "הושלמה" in r3.reply_text

    # Step 4: normal message -> should proceed to regular processing
    r4 = await processor.process(
        _text_payload(wamid="wamid-full-4", phone=phone, text="create ad for milk")
    )
    assert r4.deterministic_action != "onboarding"
    assert r4.draft_created is True


async def test_no_drafts_created_during_onboarding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001009"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(
        session_factory, operator_photo_ingestor=FakePhotoIngestor()
    )

    # Three onboarding messages
    await processor.process(
        _text_payload(wamid="wamid-nd-1", phone=phone, text="hi")
    )
    await processor.process(
        _text_payload(wamid="wamid-nd-2", phone=phone, text="Test Biz")
    )
    await processor.process(
        _image_payload(wamid="wamid-nd-3", phone=phone)
    )

    # No drafts should exist after onboarding messages
    # (the 3rd message completes onboarding but doesn't create a draft)
    assert await _count_drafts(session_factory) == 0


# ---------------------------------------------------------------------------
# Tests: Fully onboarded operator bypasses gate
# ---------------------------------------------------------------------------


async def test_onboarded_operator_bypasses_gate(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001010"
    await _seed_operator(
        session_factory,
        phone,
        business_name="Already Set",
        logo_url="https://example.com/logo.png",
    )
    processor = InboundTaskProcessor(session_factory)

    result = await processor.process(
        _text_payload(wamid="wamid-bp-1", phone=phone, text="create ad")
    )

    assert result.deterministic_action != "onboarding"
    assert result.draft_created is True


# ---------------------------------------------------------------------------
# Tests: History preservation
# ---------------------------------------------------------------------------


async def test_onboarding_history_preserved(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001011"
    await _seed_operator(session_factory, phone)
    processor = InboundTaskProcessor(session_factory)

    await processor.process(
        _text_payload(wamid="wamid-hist-1", phone=phone, text="hello")
    )
    await processor.process(
        _text_payload(wamid="wamid-hist-2", phone=phone, text="My Biz")
    )

    session_obj = await _get_session(session_factory, phone)
    assert session_obj is not None
    # 2 messages = 2 user + 2 assistant = 4 history entries
    assert len(session_obj.history) == 4
    assert session_obj.history[0]["role"] == "user"
    assert session_obj.history[0]["text"] == "hello"
    assert session_obj.history[1]["role"] == "assistant"
    assert session_obj.history[2]["role"] == "user"
    assert session_obj.history[2]["text"] == "My Biz"
    assert session_obj.history[3]["role"] == "assistant"


# ---------------------------------------------------------------------------
# Tests: Partial onboarding (admin pre-set name)
# ---------------------------------------------------------------------------


async def test_admin_preset_name_skips_to_logo_step(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500001012"
    await _seed_operator(
        session_factory, phone, business_name="Admin Set Name"
    )
    processor = InboundTaskProcessor(session_factory)

    # First message should ask for logo, not name
    result = await processor.process(
        _text_payload(wamid="wamid-partial-1", phone=phone, text="hello")
    )

    assert result.deterministic_action == "onboarding"
    assert "לוגו" in result.reply_text
    assert "תמונה" in result.reply_text
