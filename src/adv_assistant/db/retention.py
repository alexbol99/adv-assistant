import calendar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from adv_assistant.db.enums import AdDraftStatus
from adv_assistant.db.models import (
    AdDraft,
    AuditEvent,
    ConversationSession,
    ProcessedInboundMessage,
)


@dataclass(slots=True)
class RetentionPolicy:
    processed_message_days: int = 30
    draft_days: int = 90
    session_days: int = 90
    audit_months: int = 13


@dataclass(slots=True)
class RetentionResult:
    processed_inbound_deleted: int = 0
    draft_deleted: int = 0
    session_deleted: int = 0
    audit_deleted: int = 0


def subtract_calendar_months(value: datetime, months: int) -> datetime:
    if months < 0:
        msg = "months must be >= 0"
        raise ValueError(msg)
    if months == 0:
        return value

    year = value.year
    month = value.month - months

    while month <= 0:
        year -= 1
        month += 12

    max_day = calendar.monthrange(year, month)[1]
    day = min(value.day, max_day)
    return value.replace(year=year, month=month, day=day)


async def run_retention_jobs(
    session: AsyncSession,
    *,
    policy: RetentionPolicy | None = None,
    now: datetime | None = None,
) -> RetentionResult:
    retention = policy or RetentionPolicy()
    current_time = now or datetime.now(UTC)

    cutoff_processed = current_time - timedelta(days=retention.processed_message_days)
    cutoff_draft = current_time - timedelta(days=retention.draft_days)
    cutoff_session = current_time - timedelta(days=retention.session_days)
    cutoff_audit = subtract_calendar_months(current_time, retention.audit_months)

    result = RetentionResult()

    processed_result = await session.execute(
        delete(ProcessedInboundMessage).where(
            ProcessedInboundMessage.processed_at < cutoff_processed
        )
    )
    result.processed_inbound_deleted = processed_result.rowcount or 0

    session_result = await session.execute(
        delete(ConversationSession).where(ConversationSession.last_active_at < cutoff_session)
    )
    result.session_deleted = session_result.rowcount or 0

    draft_result = await session.execute(
        delete(AdDraft).where(
            AdDraft.updated_at < cutoff_draft,
            AdDraft.status.in_(
                [
                    AdDraftStatus.DRAFT,
                    AdDraftStatus.GENERATING,
                    AdDraftStatus.PREVIEW_READY,
                    AdDraftStatus.APPROVED,
                ]
            ),
        )
    )
    result.draft_deleted = draft_result.rowcount or 0

    audit_result = await session.execute(
        delete(AuditEvent).where(AuditEvent.timestamp < cutoff_audit)
    )
    result.audit_deleted = audit_result.rowcount or 0

    await session.flush()
    return result
