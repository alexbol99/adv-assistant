import pytest

from adv_assistant.tasks_auth import OidcTaskRequestAuthorizer

pytestmark = pytest.mark.anyio


async def test_authorizer_rejects_end_user_when_allowlist_not_configured(monkeypatch) -> None:
    def fake_verify(token: str, request_adapter, audience: str) -> dict[str, object]:
        return {
            "iss": "https://accounts.google.com",
            "email": "user@example.com",
            "email_verified": True,
        }

    monkeypatch.setattr(
        "adv_assistant.tasks_auth.id_token.verify_oauth2_token",
        fake_verify,
    )
    authorizer = OidcTaskRequestAuthorizer(audience="https://example.run.app/tasks/process-message")

    assert await authorizer.is_authorized("Bearer token-value") is False


async def test_authorizer_accepts_service_account_when_allowlist_not_configured(
    monkeypatch,
) -> None:
    def fake_verify(token: str, request_adapter, audience: str) -> dict[str, object]:
        return {
            "iss": "https://accounts.google.com",
            "email": "tasks-caller@ads-assistant-488908.iam.gserviceaccount.com",
            "email_verified": True,
        }

    monkeypatch.setattr(
        "adv_assistant.tasks_auth.id_token.verify_oauth2_token",
        fake_verify,
    )
    authorizer = OidcTaskRequestAuthorizer(audience="https://example.run.app/tasks/process-message")

    assert await authorizer.is_authorized("Bearer token-value") is True


async def test_authorizer_rejects_mismatched_allowlisted_service_account(monkeypatch) -> None:
    def fake_verify(token: str, request_adapter, audience: str) -> dict[str, object]:
        return {
            "iss": "https://accounts.google.com",
            "email": "other-sa@ads-assistant-488908.iam.gserviceaccount.com",
            "email_verified": True,
        }

    monkeypatch.setattr(
        "adv_assistant.tasks_auth.id_token.verify_oauth2_token",
        fake_verify,
    )
    authorizer = OidcTaskRequestAuthorizer(
        audience="https://example.run.app/tasks/process-message",
        allowed_service_account_email="tasks-caller@ads-assistant-488908.iam.gserviceaccount.com",
    )

    assert await authorizer.is_authorized("Bearer token-value") is False
