from typing import Protocol

import httpx


class WhatsAppClient(Protocol):
    async def send_text(self, *, to_phone: str, message: str) -> None: ...

    async def close(self) -> None: ...


class NoopWhatsAppClient:
    async def send_text(self, *, to_phone: str, message: str) -> None:
        return None

    async def close(self) -> None:
        return None


class MetaWhatsAppClient:
    def __init__(
        self,
        *,
        access_token: str,
        phone_number_id: str,
        graph_api_version: str,
        timeout_seconds: float = 10.0,
    ) -> None:
        self._phone_number_id = phone_number_id
        self._url = (
            f"https://graph.facebook.com/{graph_api_version}/{self._phone_number_id}/messages"
        )
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def send_text(self, *, to_phone: str, message: str) -> None:
        response = await self._client.post(
            self._url,
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "text",
                "text": {"body": message},
            },
        )
        response.raise_for_status()

    async def close(self) -> None:
        await self._client.aclose()
