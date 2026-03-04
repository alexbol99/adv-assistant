import json
import struct
from io import BytesIO

import httpx
import pytest
from PIL import Image

from adv_assistant.cms_cityscreen import CityScreenCMSPublisher, _normalize_image_for_cityscreen

pytestmark = pytest.mark.anyio


def _build_png(
    *,
    width: int,
    height: int,
    color: tuple[int, int, int, int] = (0, 128, 255, 255),
) -> bytes:
    image = Image.new("RGBA", (width, height), color)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _read_png_size(data: bytes) -> tuple[int, int]:
    if not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("not png")
    return struct.unpack(">II", data[16:24])


def _extract_first_png_size_from_multipart(data: bytes) -> tuple[int, int] | None:
    index = data.find(b"\x89PNG\r\n\x1a\n")
    if index < 0:
        return None
    return _read_png_size(data[index : index + 24])


def test_normalize_image_for_cityscreen_resizes_to_expected_resolution() -> None:
    source = _build_png(width=1376, height=768)

    prepared = _normalize_image_for_cityscreen(
        content=source,
        content_type="image/png",
        expected_width=1920,
        expected_height=1080,
    )

    assert prepared.transformed is True
    assert prepared.source_width == 1376
    assert prepared.source_height == 768
    assert prepared.width == 1920
    assert prepared.height == 1080
    assert prepared.content_type == "image/png"
    assert _read_png_size(prepared.content) == (1920, 1080)


def test_normalize_image_for_cityscreen_keeps_expected_resolution_as_is() -> None:
    source = _build_png(width=1920, height=1080)

    prepared = _normalize_image_for_cityscreen(
        content=source,
        content_type="image/png",
        expected_width=1920,
        expected_height=1080,
    )

    assert prepared.transformed is False
    assert prepared.width == 1920
    assert prepared.height == 1080
    assert prepared.content == source


async def test_cityscreen_publish_normalizes_before_upload_and_uses_expected_resolution() -> None:
    generated_image_url = "https://storage.example/generated.png"
    source = _build_png(width=1376, height=768)
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and str(request.url) == generated_image_url:
            return httpx.Response(
                status_code=200,
                content=source,
                headers={"content-type": "image/png"},
            )

        if request.url.path.endswith("/api/v1.4/advertiser/upload"):
            uploaded_size = _extract_first_png_size_from_multipart(request.content)
            observed["uploaded_size"] = uploaded_size
            return httpx.Response(
                status_code=200,
                json=[{"id": 1280, "width": 1920, "height": 1080}],
            )

        if request.url.path.endswith("/advertiser/campaigns/157/advertisements"):
            observed["create_payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(status_code=200, json={"id": 975})

        if request.method == "GET" and request.url.path.endswith("/api/v1.4/playlists/139"):
            return httpx.Response(status_code=200, json={"name": "Main", "slots": []})

        if request.method == "POST" and request.url.path.endswith("/api/v1.4/playlists/139"):
            return httpx.Response(
                status_code=200,
                json={"slots": [{"id": 336, "advertisement": {"id": 975}}]},
            )

        return httpx.Response(status_code=404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    publisher = CityScreenCMSPublisher(
        base_url="https://play.eu.cityscreen.cloud",
        app_token="test-token",
        campaign_id=157,
        playlist_id=139,
        expected_width=1920,
        expected_height=1080,
        client=client,
    )

    result = await publisher.publish_generated_image(
        image_url=generated_image_url,
        title="Milk",
    )

    assert observed["uploaded_size"] == (1920, 1080)
    create_payload = observed["create_payload"]
    assert isinstance(create_payload, dict)
    assert create_payload["medias"][0]["resolutions"] == [{"width": 1920, "height": 1080}]
    assert result.file_id == 1280
    assert result.advertisement_id == 975
    assert result.slot_id == 336
    await client.aclose()
