from typing import Protocol

import httpx

from adv_assistant.media_ingest import DownloadedMedia, MediaIngestError


class WhatsAppClient(Protocol):
    async def send_text(self, *, to_phone: str, message: str) -> None: ...
    async def send_image(
        self,
        *,
        to_phone: str,
        image_url: str,
        caption: str | None = None,
    ) -> None: ...
    async def send_buttons(
        self,
        *,
        to_phone: str,
        body_text: str,
        buttons: list[tuple[str, str]],
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

    async def send_buttons(
        self,
        *,
        to_phone: str,
        body_text: str,
        buttons: list[tuple[str, str]],
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

    async def send_buttons(
        self,
        *,
        to_phone: str,
        body_text: str,
        buttons: list[tuple[str, str]],
    ) -> None:
        interactive_buttons = [
            {
                "type": "reply",
                "reply": {"id": button_id, "title": title[:20]},
            }
            for button_id, title in buttons[:3]
        ]
        if not interactive_buttons:
            raise ValueError("at least one button is required")

        response = await self._client.post(
            self._url,
            headers=self._auth_header,
            json={
                "messaging_product": "whatsapp",
                "to": to_phone,
                "type": "interactive",
                "interactive": {
                    "type": "button",
                    "body": {"text": body_text[:1024]},
                    "action": {"buttons": interactive_buttons},
                },
            },
        )
        response.raise_for_status()

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class MetaWhatsAppMediaClient:
    def __init__(
        self,
        *,
        access_token: str,
        graph_api_version: str,
        timeout_seconds: float = 15.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._graph_api_version = graph_api_version
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    async def download_media(self, *, media_id: str) -> DownloadedMedia:
        try:
            metadata_url = f"https://graph.facebook.com/{self._graph_api_version}/{media_id}"
            metadata_response = await self._client.get(metadata_url)
            metadata_response.raise_for_status()
            metadata_payload = metadata_response.json()
            download_url = metadata_payload.get("url")
            if not isinstance(download_url, str) or not download_url.strip():
                raise MediaIngestError("Meta media response does not include download URL")

            media_response = await self._client.get(download_url)
            media_response.raise_for_status()
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            raise MediaIngestError(f"Meta media download failed: {exc}") from exc

        metadata_content_type = metadata_payload.get("mime_type")
        response_content_type = media_response.headers.get("content-type")
        resolved_content_type = _resolve_content_type(
            response_content_type=response_content_type,
            metadata_content_type=metadata_content_type
            if isinstance(metadata_content_type, str)
            else None,
        )
        return DownloadedMedia(content=media_response.content, content_type=resolved_content_type)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _resolve_content_type(
    *,
    response_content_type: str | None,
    metadata_content_type: str | None,
) -> str:
    if response_content_type:
        return response_content_type.split(";")[0].strip().lower()
    if metadata_content_type:
        return metadata_content_type.split(";")[0].strip().lower()
    return "application/octet-stream"
