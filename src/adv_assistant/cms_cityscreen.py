import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlparse

import httpx

logger = logging.getLogger(__name__)


class CMSPublishError(RuntimeError):
    pass


@dataclass(slots=True)
class CMSPublishResult:
    file_id: int
    advertisement_id: int
    slot_id: int | None


class CMSPublisher(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def publish_generated_image(
        self,
        *,
        image_url: str,
        title: str,
    ) -> CMSPublishResult: ...

    async def close(self) -> None: ...


class NoopCMSPublisher:
    @property
    def enabled(self) -> bool:
        return False

    async def publish_generated_image(
        self,
        *,
        image_url: str,
        title: str,
    ) -> CMSPublishResult:
        raise CMSPublishError("CMS publishing is not configured")

    async def close(self) -> None:
        return None


class CityScreenCMSPublisher:
    def __init__(
        self,
        *,
        base_url: str,
        app_token: str,
        campaign_id: int,
        playlist_id: int,
        picture_duration_ms: int = 10000,
        timeout_seconds: float = 30.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._campaign_id = campaign_id
        self._playlist_id = playlist_id
        self._picture_duration_ms = picture_duration_ms
        self._headers = {"x-app-token": app_token}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def enabled(self) -> bool:
        return True

    async def publish_generated_image(
        self,
        *,
        image_url: str,
        title: str,
    ) -> CMSPublishResult:
        self._debug(
            "publish start campaign_id=%s playlist_id=%s image_url=%s",
            self._campaign_id,
            self._playlist_id,
            image_url,
        )
        image_bytes, content_type = await self._download_image(image_url=image_url)
        upload_file_name = _guess_upload_filename(image_url=image_url, content_type=content_type)
        upload_info = await self._upload_picture(
            file_name=upload_file_name,
            content=image_bytes,
            content_type=content_type,
        )
        file_id = _as_int(upload_info.get("id"), field="upload.id")
        width = _as_int(upload_info.get("width") or 1920, field="upload.width")
        height = _as_int(upload_info.get("height") or 1080, field="upload.height")
        self._debug(
            "upload done file_id=%s width=%s height=%s",
            file_id,
            width,
            height,
        )

        advertisement_id = await self._create_picture_advertisement(
            file_id=file_id,
            width=width,
            height=height,
            title=title,
        )
        self._debug("advertisement created advertisement_id=%s", advertisement_id)

        slot_id = await self._append_advertisement_to_playlist(advertisement_id=advertisement_id)
        self._debug("playlist append done slot_id=%s", slot_id)
        return CMSPublishResult(
            file_id=file_id,
            advertisement_id=advertisement_id,
            slot_id=slot_id,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _download_image(self, *, image_url: str) -> tuple[bytes, str]:
        response = await self._client.get(image_url)
        _raise_for_status(response, "download generated image")
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if not content_type:
            content_type = "image/jpeg"
        if not content_type.startswith("image/"):
            raise CMSPublishError(
                f"Generated image URL returned unsupported content-type: {content_type}"
            )
        if not response.content:
            raise CMSPublishError("Generated image URL returned empty content")
        return response.content, content_type

    async def _upload_picture(
        self,
        *,
        file_name: str,
        content: bytes,
        content_type: str,
    ) -> dict[str, Any]:
        url = f"{self._base_url}/api/v1.4/advertiser/upload"
        files = [
            ("file", (file_name, content, content_type)),
            ("type", (None, "picture")),
        ]
        response = await self._client.post(url, headers=self._headers, files=files)
        _raise_for_status(response, "upload picture")
        payload = _safe_json(response, "upload picture")
        if isinstance(payload, list):
            if not payload:
                raise CMSPublishError("upload picture returned empty list")
            first_item = payload[0]
            if not isinstance(first_item, dict):
                raise CMSPublishError("upload picture returned invalid list payload")
            return first_item
        if isinstance(payload, dict):
            return payload
        raise CMSPublishError("upload picture returned unexpected payload shape")

    async def _create_picture_advertisement(
        self,
        *,
        file_id: int,
        width: int,
        height: int,
        title: str,
    ) -> int:
        url = f"{self._base_url}/api/v1.4/advertiser/campaigns/{self._campaign_id}/advertisements"
        payload = {
            "title": title[:120],
            "type": "picture",
            "cost": 0,
            "duration": self._picture_duration_ms,
            "medias": [
                {
                    "fileId": file_id,
                    "resolutions": [{"width": width, "height": height}],
                }
            ],
        }
        response = await self._client.put(url, headers=self._headers, json=payload)
        _raise_for_status(response, "create advertisement")
        body = _safe_json(response, "create advertisement")
        if not isinstance(body, dict):
            raise CMSPublishError("create advertisement returned invalid payload")
        return _as_int(body.get("id"), field="advertisement.id")

    async def _append_advertisement_to_playlist(self, *, advertisement_id: int) -> int | None:
        playlist = await self._load_playlist()
        playlist_name = playlist.get("name")
        if not isinstance(playlist_name, str) or not playlist_name.strip():
            raise CMSPublishError("playlist response did not include valid name")

        raw_slots = playlist.get("slots")
        if not isinstance(raw_slots, list):
            raise CMSPublishError("playlist response did not include slot list")

        slots_payload: list[dict[str, Any]] = []
        max_weight = -1
        for slot in raw_slots:
            if not isinstance(slot, dict):
                continue
            parsed_slot = _normalize_playlist_slot(slot)
            if parsed_slot is None:
                continue
            slots_payload.append(parsed_slot)
            max_weight = max(max_weight, parsed_slot["weight"])

        slots_payload.append(
            {
                "id": 0,
                "type": "fixed",
                "advertisement": advertisement_id,
                "weight": max_weight + 1,
                "duration": None,
            }
        )
        update_payload = {"name": playlist_name, "slots": slots_payload}
        update_url = f"{self._base_url}/api/v1.4/playlists/{self._playlist_id}"
        update_response = await self._client.post(
            update_url,
            headers=self._headers,
            json=update_payload,
        )
        _raise_for_status(update_response, "append to playlist")
        body = _safe_json(update_response, "append to playlist")
        if not isinstance(body, dict):
            raise CMSPublishError("append to playlist returned invalid payload")

        updated_slots = body.get("slots")
        if not isinstance(updated_slots, list):
            return None
        for slot in updated_slots:
            if not isinstance(slot, dict):
                continue
            ad = slot.get("advertisement")
            if isinstance(ad, dict) and _as_optional_int(ad.get("id")) == advertisement_id:
                return _as_optional_int(slot.get("id"))
        return None

    async def _load_playlist(self) -> dict[str, Any]:
        url = f"{self._base_url}/api/v1.4/playlists/{self._playlist_id}"
        response = await self._client.get(url, headers=self._headers)
        _raise_for_status(response, "load playlist")
        payload = _safe_json(response, "load playlist")
        if not isinstance(payload, dict):
            raise CMSPublishError("load playlist returned invalid payload")
        return payload

    def _debug(self, message: str, *args: Any) -> None:
        rendered = message % args if args else message
        logger.info("[CityScreen] %s", rendered)


def _raise_for_status(response: httpx.Response, action: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        text = response.text[:800] if response.text else ""
        raise CMSPublishError(
            f"{action} failed: HTTP {response.status_code}; response={text}"
        ) from exc


def _safe_json(response: httpx.Response, action: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise CMSPublishError(f"{action} failed: response is not valid JSON") from exc


def _as_int(value: Any, *, field: str) -> int:
    maybe = _as_optional_int(value)
    if maybe is None:
        raise CMSPublishError(f"Missing or invalid integer field '{field}'")
    return maybe


def _as_optional_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _guess_upload_filename(*, image_url: str, content_type: str) -> str:
    parsed = urlparse(image_url)
    path = parsed.path.rsplit("/", maxsplit=1)[-1]
    if path and "." in path:
        return path
    if content_type == "image/png":
        return "generated-image.png"
    if content_type == "image/webp":
        return "generated-image.webp"
    return "generated-image.jpg"


def _normalize_playlist_slot(slot: dict[str, Any]) -> dict[str, Any] | None:
    slot_id = _as_optional_int(slot.get("id"))
    slot_type = slot.get("type")
    if not isinstance(slot_type, str) or not slot_type:
        slot_type = "fixed"
    ad_payload = slot.get("advertisement")
    ad_id: int | None = None
    if isinstance(ad_payload, dict):
        ad_id = _as_optional_int(ad_payload.get("id"))
    else:
        ad_id = _as_optional_int(ad_payload)
    if ad_id is None:
        return None

    weight = _as_optional_int(slot.get("weight"))
    if weight is None:
        weight = 0

    duration_value = slot.get("duration")
    duration: int | None
    if duration_value is None:
        duration = None
    else:
        duration = _as_optional_int(duration_value)

    return {
        "id": slot_id or 0,
        "type": slot_type,
        "advertisement": ad_id,
        "weight": weight,
        "duration": duration,
    }
