from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from adv_assistant.media_store import MediaStore, MediaStoreError


class MediaIngestError(RuntimeError):
    """Raised when inbound operator media ingest fails."""


@dataclass(slots=True)
class DownloadedMedia:
    content: bytes
    content_type: str


@dataclass(slots=True)
class IngestedOperatorPhoto:
    public_url: str
    object_name: str
    content_type: str
    content: bytes


class WhatsAppMediaClient(Protocol):
    async def download_media(self, *, media_id: str) -> DownloadedMedia: ...

    async def close(self) -> None: ...


class OperatorPhotoIngestor(Protocol):
    async def ingest_whatsapp_image(self, *, media_id: str) -> IngestedOperatorPhoto: ...

    async def close(self) -> None: ...


class NoopWhatsAppMediaClient:
    async def download_media(self, *, media_id: str) -> DownloadedMedia:
        raise MediaIngestError("WhatsApp media client is not configured")

    async def close(self) -> None:
        return None


class NoopOperatorPhotoIngestor:
    async def ingest_whatsapp_image(self, *, media_id: str) -> IngestedOperatorPhoto:
        raise MediaIngestError("Operator photo ingest is not configured")

    async def close(self) -> None:
        return None


class DefaultOperatorPhotoIngestor:
    def __init__(
        self,
        *,
        media_client: WhatsAppMediaClient,
        media_store: MediaStore,
    ) -> None:
        self._media_client = media_client
        self._media_store = media_store

    async def ingest_whatsapp_image(self, *, media_id: str) -> IngestedOperatorPhoto:
        if not media_id.strip():
            raise MediaIngestError("media_id is required")

        downloaded = await self._media_client.download_media(media_id=media_id)
        if not downloaded.content:
            raise MediaIngestError("Downloaded media payload is empty")
        if not downloaded.content_type.startswith("image/"):
            raise MediaIngestError(
                f"Unsupported media type '{downloaded.content_type}', expected image/*"
            )

        suffix = _extension_for_content_type(downloaded.content_type)
        try:
            upload = await self._media_store.upload_bytes(
                content=downloaded.content,
                content_type=downloaded.content_type,
                suffix=suffix,
            )
        except MediaStoreError as exc:
            raise MediaIngestError(str(exc)) from exc

        return IngestedOperatorPhoto(
            public_url=upload.public_url,
            object_name=upload.object_name,
            content_type=downloaded.content_type,
            content=downloaded.content,
        )

    async def close(self) -> None:
        return None


def _extension_for_content_type(content_type: str) -> str:
    normalized = content_type.strip().lower().split(";")[0]
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/heic": ".heic",
        "image/heif": ".heif",
        "image/gif": ".gif",
    }
    return mapping.get(normalized, "")
