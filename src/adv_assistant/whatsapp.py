from typing import Protocol

import httpx


class WhatsAppClient(Protocol):
    async def send_text(self, *, to_phone: str, message: str) -> None: ...
    async def send_image(
        self,
        *,
        to_phone: str,
        image_url: str,
        caption: str | None = None,
    ) -> None: ...

    async def close(self) -> None: ...


class NoopWhatsAppClient:
    async def send_text(self, *, to_phone: str, message: str) -> None:
        return None

    async def send_image(
        self,
        *,
        to_phone: str,
        image_url: str,
        caption: str | None = None,
    ) -> None:
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
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._phone_number_id = phone_number_id
        self._url = (
            f"https://graph.facebook.com/{graph_api_version}/{self._phone_number_id}/messages"
        )
        self._auth_header = {"Authorization": f"Bearer {access_token}"}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers=self._auth_header,
        )

    async def send_text(self, *, to_phone: str, message: str) -> None:
        response = await self._client.post(
            self._url,
            headers=self._auth_header,
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "text",
                "text": {"body": message},
            },
        )
        response.raise_for_status()

    async def send_image(
        self,
        *,
        to_phone: str,
        image_url: str,
        caption: str | None = None,
    ) -> None:
        image_payload: dict[str, str] = {"link": image_url}
        if caption:
            image_payload["caption"] = caption
        response = await self._client.post(
            self._url,
            headers=self._auth_header,
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "image",
                "image": image_payload,
            },
        )
        response.raise_for_status()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
