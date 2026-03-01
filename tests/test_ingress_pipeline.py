import hashlib
import hmac
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from adv_assistant.config import Settings
from adv_assistant.db.base import Base
from adv_assistant.db.models import AuditEvent, ProcessedInboundMessage
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import session_scope
from adv_assistant.main import create_app
from adv_assistant.tasks_queue import InboundTaskPayload

pytestmark = pytest.mark.anyio


class FakeTaskEnqueuer:
    def __init__(self) -> None:
        self.payloads: list[InboundTaskPayload] = []

    async def enqueue_inbound(self, payload: InboundTaskPayload) -> None:
        self.payloads.append(payload)


class FakeWhatsAppClient:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    async def send_text(self, *, to_phone: str, message: str) -> None:
        self.messages.append((to_phone, message))

    async def close(self) -> None:
        return None


class AllowAllTaskAuthorizer:
    async def is_authorized(self, authorization_header: str | None) -> bool:
        return True


def _build_webhook_payload(*, from_phone: str, wamid: str, timestamp: datetime) -> dict[str, Any]:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": wamid,
                                    "timestamp": str(int(timestamp.timestamp())),
                                    "type": "text",
                                    "text": {"body": "hello"},
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }


def _sign_payload(secret: str, payload: dict[str, Any]) -> tuple[bytes, str]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return body, f"sha256={signature}"


@pytest.fixture()
async def phase2_app(tmp_path: Path) -> AsyncIterator:
    db_path = tmp_path / "phase2.db"
    settings = Settings(
        app_name="adv-assistant-test",
        database_url=f"sqlite+aiosqlite:///{db_path}",
        meta_verify_token="hello_verify_2025",
        meta_app_secret="phase2-test-secret",
        tasks_mode="inline",
    )
    app = create_app(settings)
    async with app.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await app.router.startup()
    yield app
    await app.router.shutdown()


@pytest.fixture()
async def phase2_client(phase2_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=phase2_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _seed_operator(session_factory, phone: str) -> None:
    async with session_scope(session_factory) as session:
        await OperatorRepository(session).create(phone=phone, active=True)


async def _count_audit_events(
    session_factory,
    *,
    action: str,
    operator_phone: str,
) -> int:
    async with session_scope(session_factory) as session:
        count_query = select(func.count(AuditEvent.id)).where(
            AuditEvent.action == action,
            AuditEvent.operator_phone == operator_phone,
        )
        result = await session.execute(count_query)
        return int(result.scalar_one())


async def test_webhook_verification_challenge_success(phase2_client: AsyncClient) -> None:
    response = await phase2_client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "hello_verify_2025",
            "hub.challenge": "1234",
        },
    )

    assert response.status_code == 200
    assert response.text == "1234"


async def test_webhook_verification_challenge_forbidden(phase2_client: AsyncClient) -> None:
    response = await phase2_client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong-token",
            "hub.challenge": "1234",
        },
    )

    assert response.status_code == 403


async def test_webhook_rejects_invalid_signature(phase2_client: AsyncClient) -> None:
    payload = _build_webhook_payload(
        from_phone="972526508861",
        wamid="wamid-invalid-signature",
        timestamp=datetime.now(tz=UTC),
    )
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    response = await phase2_client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=invalid",
        },
    )

    assert response.status_code == 401


async def test_unauthorized_rejection_sent_once_then_silent(
    phase2_app,
    phase2_client: AsyncClient,
) -> None:
    fake_sender = FakeWhatsAppClient()
    phase2_app.state.whatsapp_client = fake_sender

    now = datetime.now(tz=UTC)
    first_payload = _build_webhook_payload(
        from_phone="972526508862",
        wamid="wamid-unauth-1",
        timestamp=now,
    )
    second_payload = _build_webhook_payload(
        from_phone="972526508862",
        wamid="wamid-unauth-2",
        timestamp=now + timedelta(seconds=1),
    )

    first_body, first_sig = _sign_payload("phase2-test-secret", first_payload)
    second_body, second_sig = _sign_payload("phase2-test-secret", second_payload)

    first_response = await phase2_client.post(
        "/webhook",
        content=first_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": first_sig},
    )
    second_response = await phase2_client.post(
        "/webhook",
        content=second_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": second_sig},
    )

    assert first_response.status_code == 200
    assert second_response.status_code == 200
    assert len(fake_sender.messages) == 1
    assert fake_sender.messages[0][0] == "+972526508862"
    assert (
        await _count_audit_events(
            phase2_app.state.session_factory,
            action="unauthorized_rejection_sent",
            operator_phone="+972526508862",
        )
        == 1
    )


async def test_authorized_event_enqueued_and_stale_event_rejected(
    phase2_app,
    phase2_client: AsyncClient,
) -> None:
    fake_enqueuer = FakeTaskEnqueuer()
    phase2_app.state.task_enqueuer = fake_enqueuer
    await _seed_operator(phase2_app.state.session_factory, "+972526508861")

    now = datetime.now(tz=UTC)
    fresh_payload = _build_webhook_payload(
        from_phone="972526508861",
        wamid="wamid-fresh",
        timestamp=now,
    )
    stale_payload = _build_webhook_payload(
        from_phone="972526508861",
        wamid="wamid-stale",
        timestamp=now - timedelta(minutes=10),
    )

    fresh_body, fresh_sig = _sign_payload("phase2-test-secret", fresh_payload)
    stale_body, stale_sig = _sign_payload("phase2-test-secret", stale_payload)

    fresh_response = await phase2_client.post(
        "/webhook",
        content=fresh_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": fresh_sig},
    )
    stale_response = await phase2_client.post(
        "/webhook",
        content=stale_body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": stale_sig},
    )

    assert fresh_response.status_code == 200
    assert stale_response.status_code == 200
    assert len(fake_enqueuer.payloads) == 1
    assert fake_enqueuer.payloads[0].wamid == "wamid-fresh"
    assert (
        await _count_audit_events(
            phase2_app.state.session_factory,
            action="webhook_replay_rejected",
            operator_phone="+972526508861",
        )
        == 1
    )


async def test_task_endpoint_rejects_non_oidc_caller(phase2_client: AsyncClient) -> None:
    payload = {
        "wamid": "wamid-task-1",
        "operator_phone": "+972526508861",
        "raw_message": {"type": "text"},
    }
    response = await phase2_client.post("/tasks/process-message", json=payload)
    assert response.status_code == 401


async def test_task_endpoint_deduplicates_wamid(
    phase2_app,
    phase2_client: AsyncClient,
) -> None:
    phase2_app.state.task_authorizer = AllowAllTaskAuthorizer()
    payload = {
        "wamid": "wamid-task-dedup",
        "operator_phone": "+972526508861",
        "raw_message": {"type": "text"},
    }

    first_response = await phase2_client.post(
        "/tasks/process-message",
        json=payload,
        headers={"Authorization": "Bearer test"},
    )
    second_response = await phase2_client.post(
        "/tasks/process-message",
        json=payload,
        headers={"Authorization": "Bearer test"},
    )

    assert first_response.status_code == 200
    assert first_response.json() == {"status": "processed"}
    assert second_response.status_code == 200
    assert second_response.json() == {"status": "duplicate_skipped"}

    async with session_scope(phase2_app.state.session_factory) as session:
        assert isinstance(session, AsyncSession)
        count_query = select(func.count(ProcessedInboundMessage.id)).where(
            ProcessedInboundMessage.wamid == "wamid-task-dedup"
        )
        result = await session.execute(count_query)
        assert int(result.scalar_one()) == 1
