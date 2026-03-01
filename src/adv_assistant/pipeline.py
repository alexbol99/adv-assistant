from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.repositories import AuditEventRepository, ProcessedInboundMessageRepository
from adv_assistant.db.session import session_scope
from adv_assistant.tasks_queue import InboundTaskPayload


@dataclass(slots=True)
class ProcessInboundResult:
    duplicate: bool

    @property
    def status(self) -> str:
        if self.duplicate:
            return "duplicate_skipped"
        return "processed"


class InboundTaskProcessor:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def process(self, payload: InboundTaskPayload) -> ProcessInboundResult:
        async with session_scope(self._session_factory) as session:
            processed_repo = ProcessedInboundMessageRepository(session)
            audit_repo = AuditEventRepository(session)

            inserted = await processed_repo.mark_processed(
                wamid=payload.wamid,
                operator_phone=payload.operator_phone,
            )
            if not inserted:
                return ProcessInboundResult(duplicate=True)

            await audit_repo.log(
                actor="system",
                action="inbound_message_processed",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid},
            )
            return ProcessInboundResult(duplicate=False)
