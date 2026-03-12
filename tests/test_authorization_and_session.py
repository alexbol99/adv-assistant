import asyncio
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.base import Base
from adv_assistant.db.models import AdDraft, ConversationSession
from adv_assistant.db.repositories import AdDraftRepository, OperatorRepository
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.draft_service import DraftService
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


@pytest.fixture()
async def phase3_session_factory(
    tmp_path: Path,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    db_path = tmp_path / "phase3.db"
    engine = create_engine(f"sqlite+aiosqlite:///{db_path}")

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    session_factory = create_session_factory(engine)
    yield session_factory

    await engine.dispose()


async def _seed_operator(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    phone: str,
    active: bool = True,
) -> None:
    async with session_scope(session_factory) as session:
        await OperatorRepository(session).create(
            phone=phone,
            active=active,
            business_name="Test Biz",
            logo_url="https://example.com/logo.png",
        )


async def _count_rows(
    session_factory: async_sessionmaker[AsyncSession],
    model: type,
) -> int:
    async with session_scope(session_factory) as session:
        result = await session.execute(select(func.count()).select_from(model))
        return int(result.scalar_one())


def _text_payload(*, wamid: str, phone: str, text: str) -> InboundTaskPayload:
    return InboundTaskPayload(
        wamid=wamid,
        operator_phone=phone,
        raw_message={"type": "text", "text": {"body": text}},
    )


async def test_unknown_operator_cannot_create_session_or_draft(
    phase3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    processor = InboundTaskProcessor(phase3_session_factory)

    result = await processor.process(
        _text_payload(wamid="wamid-unknown", phone="+972500000300", text="hello")
    )

    assert result.status == "unauthorized_operator"
    assert await _count_rows(phase3_session_factory, ConversationSession) == 0
    assert await _count_rows(phase3_session_factory, AdDraft) == 0


async def test_authorized_first_message_creates_session_and_draft(
    phase3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000301"
    await _seed_operator(phase3_session_factory, phone=phone)
    processor = InboundTaskProcessor(phase3_session_factory)

    result = await processor.process(_text_payload(wamid="wamid-1", phone=phone, text="shalom"))

    assert result.status == "processed"
    assert result.session_created is True
    assert result.draft_created is True

    async with session_scope(phase3_session_factory) as session:
        session_query = select(ConversationSession).where(
            ConversationSession.operator_phone == phone
        )
        session_obj = (await session.execute(session_query)).scalar_one()
        assert session_obj.current_draft_id is not None
        assert len(session_obj.history) == 2
        assert session_obj.history[0]["role"] == "user"
        assert session_obj.history[0]["text"] == "shalom"
        assert session_obj.history[1]["role"] == "assistant"

        draft = await session.get(AdDraft, session_obj.current_draft_id)
        assert draft is not None
        assert draft.operator_phone == phone


async def test_each_operator_gets_private_draft(
    phase3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone_a = "+972500000302"
    phone_b = "+972500000303"
    await _seed_operator(phase3_session_factory, phone=phone_a)
    await _seed_operator(phase3_session_factory, phone=phone_b)
    processor = InboundTaskProcessor(phase3_session_factory)

    await processor.process(_text_payload(wamid="wamid-a", phone=phone_a, text="first"))
    await processor.process(_text_payload(wamid="wamid-b", phone=phone_b, text="second"))

    async with session_scope(phase3_session_factory) as session:
        session_rows = await session.execute(select(ConversationSession))
        sessions = list(session_rows.scalars().all())
        assert len(sessions) == 2

        for session_obj in sessions:
            assert session_obj.current_draft_id is not None
            draft = await session.get(AdDraft, session_obj.current_draft_id)
            assert draft is not None
            assert draft.operator_phone == session_obj.operator_phone

        assert sessions[0].current_draft_id != sessions[1].current_draft_id


async def test_first_write_wins_for_versioned_draft_updates(
    phase3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    phone = "+972500000304"
    await _seed_operator(phase3_session_factory, phone=phone)
    processor = InboundTaskProcessor(phase3_session_factory)
    await processor.process(_text_payload(wamid="wamid-init", phone=phone, text="init"))

    async with session_scope(phase3_session_factory) as session:
        session_query = select(ConversationSession).where(
            ConversationSession.operator_phone == phone
        )
        session_obj = (await session.execute(session_query)).scalar_one()
        draft_id = session_obj.current_draft_id
        assert draft_id is not None

    draft_service = DraftService(phase3_session_factory)
    first_result, second_result = await asyncio.gather(
        draft_service.update_promo_text(
            operator_phone=phone,
            draft_id=draft_id,
            expected_version=1,
            promo_text="A",
        ),
        draft_service.update_promo_text(
            operator_phone=phone,
            draft_id=draft_id,
            expected_version=1,
            promo_text="B",
        ),
    )

    statuses = sorted([first_result.status, second_result.status])
    assert statuses == ["stale", "updated"]
    stale_result = first_result if first_result.status == "stale" else second_result
    assert (
        stale_result.user_message == "This draft was already changed. Please refresh and try again."
    )

    async with session_scope(phase3_session_factory) as session:
        draft = await session.get(AdDraft, draft_id)
        assert draft is not None
        assert draft.version == 2
        assert draft.promo_text in {"A", "B"}


async def test_operator_cannot_update_another_operators_draft(
    phase3_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    owner_phone = "+972500000305"
    foreign_phone = "+972500000306"
    await _seed_operator(phase3_session_factory, phone=owner_phone)
    await _seed_operator(phase3_session_factory, phone=foreign_phone)

    draft_id = uuid.uuid4()
    async with session_scope(phase3_session_factory) as session:
        draft = await AdDraftRepository(session).create(operator_phone=owner_phone)
        draft_id = draft.id

    result = await DraftService(phase3_session_factory).update_promo_text(
        operator_phone=foreign_phone,
        draft_id=draft_id,
        expected_version=1,
        promo_text="not allowed",
    )
    assert result.status == "forbidden_or_missing"
    assert result.user_message == "You are not allowed to update this draft."

    async with session_scope(phase3_session_factory) as session:
        draft = await session.get(AdDraft, draft_id)
        assert draft is not None
        assert draft.version == 1
        assert draft.promo_text is None
