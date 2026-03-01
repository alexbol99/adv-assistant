from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.base import utcnow
from adv_assistant.db.enums import AdDraftStatus
from adv_assistant.db.repositories import (
    AdDraftRepository,
    AuditEventRepository,
    ConversationSessionRepository,
    OperatorRepository,
    ProcessedInboundMessageRepository,
)
from adv_assistant.db.session import session_scope
from adv_assistant.tasks_queue import InboundTaskPayload


@dataclass(slots=True)
class ProcessInboundResult:
    duplicate: bool
    unauthorized_operator: bool = False
    session_created: bool = False
    draft_created: bool = False

    @property
    def status(self) -> str:
        if self.duplicate:
            return "duplicate_skipped"
        if self.unauthorized_operator:
            return "unauthorized_operator"
        return "processed"


def _extract_text(raw_message: dict[str, Any]) -> str | None:
    if raw_message.get("type") != "text":
        return None
    text_payload = raw_message.get("text", {})
    body = text_payload.get("body")
    if not isinstance(body, str):
        return None
    stripped = body.strip()
    return stripped or None


class InboundTaskProcessor:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def process(self, payload: InboundTaskPayload) -> ProcessInboundResult:
        async with session_scope(self._session_factory) as session:
            operator_repo = OperatorRepository(session)
            session_repo = ConversationSessionRepository(session)
            draft_repo = AdDraftRepository(session)
            processed_repo = ProcessedInboundMessageRepository(session)
            audit_repo = AuditEventRepository(session)

            inserted = await processed_repo.mark_processed(
                wamid=payload.wamid,
                operator_phone=payload.operator_phone,
            )
            if not inserted:
                return ProcessInboundResult(duplicate=True)

            operator = await operator_repo.get_by_phone(payload.operator_phone)
            if operator is None or not operator.active:
                await audit_repo.log(
                    actor="system",
                    action="inbound_unauthorized_operator_skipped",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
                return ProcessInboundResult(duplicate=False, unauthorized_operator=True)

            now = utcnow()
            session_obj = await session_repo.get_by_operator_phone(payload.operator_phone)
            session_created = session_obj is None

            history: list[dict[str, str]] = []
            current_draft_id = None
            if session_obj is not None:
                history = list(session_obj.history)
                current_draft_id = session_obj.current_draft_id

            incoming_text = _extract_text(payload.raw_message)
            if incoming_text is not None:
                history.append(
                    {
                        "role": "user",
                        "text": incoming_text,
                        "wamid": payload.wamid,
                    }
                )

            draft_created = False
            if current_draft_id is not None:
                existing_draft = await draft_repo.get_by_id(current_draft_id)
                if (
                    existing_draft is None
                    or existing_draft.operator_phone != payload.operator_phone
                ):
                    current_draft_id = None
                    await audit_repo.log(
                        actor="system",
                        action="session_draft_owner_mismatch",
                        operator_phone=payload.operator_phone,
                        metadata={"wamid": payload.wamid},
                    )

            if current_draft_id is None:
                created_draft = await draft_repo.create(
                    operator_phone=payload.operator_phone,
                    status=AdDraftStatus.DRAFT,
                )
                current_draft_id = created_draft.id
                draft_created = True

            await session_repo.create_or_update(
                operator_phone=payload.operator_phone,
                language=operator.language if session_created else None,
                history=history,
                current_draft_id=current_draft_id,
                last_active_at=now,
            )

            if session_created:
                await audit_repo.log(
                    actor="system",
                    action="conversation_session_created",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
            if draft_created:
                await audit_repo.log(
                    actor="system",
                    action="operator_draft_created",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid, "draft_id": str(current_draft_id)},
                )

            await audit_repo.log(
                actor="system",
                action="inbound_message_processed",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "session_created": session_created,
                    "draft_created": draft_created,
                },
            )
            return ProcessInboundResult(
                duplicate=False,
                unauthorized_operator=False,
                session_created=session_created,
                draft_created=draft_created,
            )
