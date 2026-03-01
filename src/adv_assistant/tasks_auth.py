from typing import Protocol

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from starlette.concurrency import run_in_threadpool


class TaskRequestAuthorizer(Protocol):
    async def is_authorized(self, authorization_header: str | None) -> bool: ...


class RejectAllTaskRequestAuthorizer:
    async def is_authorized(self, authorization_header: str | None) -> bool:
        return False


class OidcTaskRequestAuthorizer:
    def __init__(
        self,
        *,
        audience: str,
        allowed_service_account_email: str | None = None,
        request_adapter: google_requests.Request | None = None,
    ) -> None:
        self._audience = audience
        self._allowed_service_account_email = allowed_service_account_email
        self._request_adapter = request_adapter or google_requests.Request()

    async def is_authorized(self, authorization_header: str | None) -> bool:
        if not authorization_header or not authorization_header.startswith("Bearer "):
            return False

        token = authorization_header[len("Bearer ") :].strip()
        if not token:
            return False

        try:
            claims = await run_in_threadpool(
                id_token.verify_oauth2_token,
                token,
                self._request_adapter,
                self._audience,
            )
        except Exception:
            return False

        issuer = claims.get("iss")
        if issuer not in {"https://accounts.google.com", "accounts.google.com"}:
            return False

        email = claims.get("email")
        if self._allowed_service_account_email and email != self._allowed_service_account_email:
            return False

        email_verified = claims.get("email_verified")
        if email_verified is False:
            return False

        return True
