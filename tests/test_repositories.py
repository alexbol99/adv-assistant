from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from adv_assistant.db.base import utcnow
from adv_assistant.db.enums import AdDraftStatus
from adv_assistant.db.models import (
    AdDraft,
    AuditEvent,
    ConversationSession,
    ProcessedInboundMessage,
)
from adv_assistant.db.repositories import (
    AdDraftRepository,
    AuditEventRepository,
    ConversationSessionRepository,
    OperatorRepository,
    ProcessedInboundMessageRepository,
    PublishedAdRepository,
    SystemConfigRepository,
)
from adv_assistant.db.retention import RetentionPolicy, run_retention_jobs

pytestmark = pytest.mark.anyio


async def test_crud_repositories(db_session: AsyncSession) -> None:
    operator_repo = OperatorRepository(db_session)
    session_repo = ConversationSessionRepository(db_session)
    draft_repo = AdDraftRepository(db_session)
    published_repo = PublishedAdRepository(db_session)
    config_repo = SystemConfigRepository(db_session)
    audit_repo = AuditEventRepository(db_session)
    processed_repo = ProcessedInboundMessageRepository(db_session)

    operator = await operator_repo.create(phone="+972500000001", display_name="Ops 1")
    assert operator.phone == "+972500000001"

    fetched_operator = await operator_repo.get_by_phone("+972500000001")
    assert fetched_operator is not None
    assert fetched_operator.active is True

    updated_branding = await operator_repo.update_branding(
        operator.phone,
        business_name="Super Market",
        logo_url="https://example.com/logo.png",
        brand_colors=["#FF0000", "#00FF00"],
        store_type="grocery",
        creative_guidance="Clean layout with large readable price",
        language="en",
    )
    assert updated_branding is True
    refreshed_operator = await operator_repo.get_by_phone(operator.phone)
    assert refreshed_operator is not None
    assert refreshed_operator.business_name == "Super Market"
    assert refreshed_operator.logo_url == "https://example.com/logo.png"
    assert refreshed_operator.brand_colors == ["#FF0000", "#00FF00"]
    assert refreshed_operator.store_type == "grocery"
    assert refreshed_operator.creative_guidance == "Clean layout with large readable price"
    assert refreshed_operator.language == "en"

    updated_mapping = await operator_repo.update_cms_mapping(
        operator.phone,
        cms_campaign_id=157,
        cms_playlist_id=139,
    )
    assert updated_mapping is True
    refreshed_operator = await operator_repo.get_by_phone(operator.phone)
    assert refreshed_operator is not None
    assert refreshed_operator.cms_campaign_id == 157
    assert refreshed_operator.cms_playlist_id == 139

    active_operators = await operator_repo.list_active()
    assert len(active_operators) == 1

    conversation = await session_repo.create_or_update(
        operator_phone=operator.phone,
        language="he",
        history=[{"role": "user", "text": "hello"}],
        last_active_at=utcnow(),
    )
    assert conversation.operator_phone == operator.phone

    draft = await draft_repo.create(
        operator_phone=operator.phone,
        product_name="Milk",
        promo_text="Fresh 1L",
        status=AdDraftStatus.DRAFT,
    )
    assert draft.version == 1

    updated_draft = await draft_repo.update_with_version(
        draft_id=draft.id,
        expected_version=1,
        promo_text="Fresh 1L - discount",
        status=AdDraftStatus.APPROVED,
    )
    assert updated_draft is not None
    assert updated_draft.version == 2
    assert updated_draft.promo_text == "Fresh 1L - discount"

    stale_update = await draft_repo.update_with_version(
        draft_id=draft.id,
        expected_version=1,
        promo_text="stale write",
    )
    assert stale_update is None

    conversation.current_draft_id = draft.id
    await db_session.flush()

    published = await published_repo.create(cms_id="cms-1", ad_draft_id=draft.id)
    assert published.cms_id == "cms-1"

    config = await config_repo.upsert(cms_base_url="https://cms.local", retention_media_days=90)
    assert config.id == 1
    assert config.cms_base_url == "https://cms.local"

    event = await audit_repo.log(actor="system", action="publish_ad", metadata={"cms_id": "cms-1"})
    assert event.action == "publish_ad"

    first_mark = await processed_repo.mark_processed(wamid="wamid-1", operator_phone=operator.phone)
    second_mark = await processed_repo.mark_processed(
        wamid="wamid-1", operator_phone=operator.phone
    )
    assert first_mark is True
    assert second_mark is False
    assert await processed_repo.exists("wamid-1") is True

    await db_session.commit()


async def test_processed_message_purge_uses_expires_at(db_session: AsyncSession) -> None:
    processed_repo = ProcessedInboundMessageRepository(db_session)
    now = utcnow()

    db_session.add_all(
        [
            ProcessedInboundMessage(
                wamid="purge-keep",
                operator_phone="+972500000200",
                processed_at=now - timedelta(days=120),
                expires_at=now + timedelta(days=1),
            ),
            ProcessedInboundMessage(
                wamid="purge-delete",
                operator_phone="+972500000201",
                processed_at=now - timedelta(days=1),
                expires_at=now - timedelta(minutes=1),
            ),
        ]
    )
    await db_session.flush()

    deleted = await processed_repo.purge_older_than(now)

    assert deleted == 1
    assert await processed_repo.exists("purge-keep") is True
    assert await processed_repo.exists("purge-delete") is False


async def test_session_repository_can_clear_nullable_fields(db_session: AsyncSession) -> None:
    operator_repo = OperatorRepository(db_session)
    session_repo = ConversationSessionRepository(db_session)
    draft_repo = AdDraftRepository(db_session)

    operator = await operator_repo.create(phone="+972500000555")
    draft = await draft_repo.create(
        operator_phone=operator.phone,
        status=AdDraftStatus.DRAFT,
    )
    initial_expiry = utcnow() + timedelta(days=1)

    created = await session_repo.create_or_update(
        operator_phone=operator.phone,
        history=[{"role": "user", "text": "hello"}],
        current_draft_id=draft.id,
        pending_upload_type="logo",
        pending_followup_question="price",
        expires_at=initial_expiry,
    )
    assert created.current_draft_id == draft.id
    assert created.pending_upload_type == "logo"
    assert created.pending_followup_question == "price"
    assert created.expires_at == initial_expiry

    cleared = await session_repo.create_or_update(
        operator_phone=operator.phone,
        current_draft_id=None,
        pending_upload_type=None,
        pending_followup_question=None,
        expires_at=None,
    )
    assert cleared.current_draft_id is None
    assert cleared.pending_upload_type is None
    assert cleared.pending_followup_question is None
    assert cleared.expires_at is None


async def test_audit_repository_lists_recent_operator_actions(db_session: AsyncSession) -> None:
    audit_repo = AuditEventRepository(db_session)

    await audit_repo.log(
        actor="system",
        action="openai_request_debug",
        operator_phone="+972500000888",
        metadata={"wamid": "w-1"},
    )
    await audit_repo.log(
        actor="system",
        action="gemini_request_debug",
        operator_phone="+972500000888",
        metadata={"wamid": "w-2"},
    )
    await audit_repo.log(
        actor="system",
        action="inbound_message_processed",
        operator_phone="+972500000888",
        metadata={"wamid": "w-3"},
    )

    rows = await audit_repo.list_recent_for_operator_actions(
        operator_phone="+972500000888",
        actions=["openai_request_debug", "gemini_request_debug"],
        limit=10,
    )

    assert len(rows) == 2
    assert {row.action for row in rows} == {"openai_request_debug", "gemini_request_debug"}


async def test_retention_jobs(db_session: AsyncSession) -> None:
    operator_repo = OperatorRepository(db_session)
    session_repo = ConversationSessionRepository(db_session)
    draft_repo = AdDraftRepository(db_session)
    processed_repo = ProcessedInboundMessageRepository(db_session)
    audit_repo = AuditEventRepository(db_session)

    operator = await operator_repo.create(phone="+972500000099")

    now = utcnow()

    old_session = await session_repo.create_or_update(
        operator_phone=operator.phone,
        last_active_at=now - timedelta(days=95),
    )
    assert old_session is not None

    fresh_session = await session_repo.create_or_update(
        operator_phone=operator.phone,
        history=[{"role": "assistant", "text": "active"}],
        last_active_at=now,
    )
    assert fresh_session is not None

    old_draft = await draft_repo.create(
        operator_phone=operator.phone,
        product_name="Old Draft",
        status=AdDraftStatus.APPROVED,
    )
    old_draft.updated_at = now - timedelta(days=95)

    published_draft = await draft_repo.create(
        operator_phone=operator.phone,
        product_name="Published Draft",
        status=AdDraftStatus.PUBLISHED,
    )
    published_draft.updated_at = now - timedelta(days=95)

    old_processed = ProcessedInboundMessage(
        wamid="old-wamid",
        operator_phone=operator.phone,
        processed_at=now - timedelta(days=40),
        expires_at=now - timedelta(days=5),
    )
    new_processed = ProcessedInboundMessage(
        wamid="new-wamid",
        operator_phone=operator.phone,
        processed_at=now,
        expires_at=now + timedelta(days=30),
    )
    old_but_unexpired_processed = ProcessedInboundMessage(
        wamid="old-but-unexpired-wamid",
        operator_phone=operator.phone,
        processed_at=now - timedelta(days=120),
        expires_at=now + timedelta(days=2),
    )
    recent_but_expired_processed = ProcessedInboundMessage(
        wamid="recent-but-expired-wamid",
        operator_phone=operator.phone,
        processed_at=now - timedelta(days=1),
        expires_at=now - timedelta(hours=1),
    )
    db_session.add_all(
        [
            old_processed,
            new_processed,
            old_but_unexpired_processed,
            recent_but_expired_processed,
        ]
    )

    await audit_repo.log(
        actor="system",
        action="delete_all",
        metadata={"kind": "old"},
        timestamp=now - timedelta(days=410),
    )
    await audit_repo.log(
        actor="system",
        action="delete_all",
        metadata={"kind": "new"},
        timestamp=now,
    )
    await db_session.flush()

    result = await run_retention_jobs(
        db_session,
        policy=RetentionPolicy(
            processed_message_days=30,
            draft_days=90,
            session_days=90,
            audit_months=13,
        ),
        now=now,
    )

    assert result.processed_inbound_deleted == 2
    assert result.draft_deleted == 1
    assert result.session_deleted == 0
    assert result.audit_deleted == 1

    remaining_processed = await processed_repo.exists("new-wamid")
    assert remaining_processed is True
    assert await processed_repo.exists("old-wamid") is False
    assert await processed_repo.exists("old-but-unexpired-wamid") is True
    assert await processed_repo.exists("recent-but-expired-wamid") is False

    remaining_old_draft = await draft_repo.get_by_id(old_draft.id)
    remaining_published_draft = await draft_repo.get_by_id(published_draft.id)
    assert remaining_old_draft is None
    assert remaining_published_draft is not None

    audit_rows = await db_session.execute(select(AuditEvent))
    assert len(list(audit_rows.scalars().all())) == 1

    session_rows = await db_session.execute(select(ConversationSession))
    assert len(list(session_rows.scalars().all())) == 1

    draft_rows = await db_session.execute(select(AdDraft))
    assert len(list(draft_rows.scalars().all())) == 1
