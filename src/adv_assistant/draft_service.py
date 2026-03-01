import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.db.repositories import AdDraftRepository
from adv_assistant.db.session import session_scope

DraftUpdateStatus = Literal["updated", "stale", "forbidden_or_missing"]


@dataclass(slots=True)
class DraftUpdateResult:
    status: DraftUpdateStatus
    draft_id: uuid.UUID
    new_version: int | None = None

    @property
    def user_message(self) -> str:
        if self.status == "updated":
            return "Draft updated."
        if self.status == "stale":
            return "This draft was already changed. Please refresh and try again."
        return "You are not allowed to update this draft."


class DraftService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def update_promo_text(
        self,
        *,
        operator_phone: str,
        draft_id: uuid.UUID,
        expected_version: int,
        promo_text: str,
    ) -> DraftUpdateResult:
        async with session_scope(self._session_factory) as session:
            draft_repo = AdDraftRepository(session)
            updated = await draft_repo.update_for_operator_with_version(
                draft_id=draft_id,
                operator_phone=operator_phone,
                expected_version=expected_version,
                promo_text=promo_text,
            )
            if updated is not None:
                return DraftUpdateResult(
                    status="updated",
                    draft_id=draft_id,
                    new_version=updated.version,
                )

            own_draft = await draft_repo.get_by_id_for_operator(draft_id, operator_phone)
            if own_draft is None:
                return DraftUpdateResult(status="forbidden_or_missing", draft_id=draft_id)

            return DraftUpdateResult(status="stale", draft_id=draft_id)
