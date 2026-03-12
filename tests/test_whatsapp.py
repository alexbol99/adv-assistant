import json

import httpx
import pytest

from adv_assistant.whatsapp import MetaWhatsAppClient

pytestmark = pytest.mark.anyio


async def test_meta_whatsapp_client_send_text_posts_text_payload() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("Authorization")
        observed["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"messages": [{"id": "msg-1"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    whatsapp = MetaWhatsAppClient(
        access_token="token",
        phone_number_id="123456",
        graph_api_version="v21.0",
        client=client,
    )

    await whatsapp.send_text(to_phone="+972526508861", message="hello")

    assert observed["url"] == "https://graph.facebook.com/v21.0/123456/messages"
    assert observed["authorization"] == "Bearer token"
    assert observed["body"] == {
        "messaging_product": "whatsapp",
        "to": "+972526508861",
        "type": "text",
        "text": {"body": "hello"},
    }
    await client.aclose()


async def test_meta_whatsapp_client_send_image_posts_image_payload_with_caption() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("Authorization")
        observed["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"messages": [{"id": "msg-2"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    whatsapp = MetaWhatsAppClient(
        access_token="token",
        phone_number_id="123456",
        graph_api_version="v21.0",
        client=client,
    )

    await whatsapp.send_image(
        to_phone="+972526508861",
        image_url="https://storage.googleapis.com/media/preview.png",
        caption="Your ad preview is ready.",
    )

    assert observed["url"] == "https://graph.facebook.com/v21.0/123456/messages"
    assert observed["authorization"] == "Bearer token"
    assert observed["body"] == {
        "messaging_product": "whatsapp",
        "to": "+972526508861",
        "type": "image",
        "image": {
            "link": "https://storage.googleapis.com/media/preview.png",
            "caption": "Your ad preview is ready.",
        },
    }
    await client.aclose()


async def test_meta_whatsapp_client_send_buttons_posts_interactive_payload() -> None:
    observed: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        observed["url"] = str(request.url)
        observed["authorization"] = request.headers.get("Authorization")
        observed["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(status_code=200, json={"messages": [{"id": "msg-3"}]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    whatsapp = MetaWhatsAppClient(
        access_token="token",
        phone_number_id="123456",
        graph_api_version="v21.0",
        client=client,
    )

    await whatsapp.send_buttons(
        to_phone="+972526508861",
        body_text="Publish now?",
        buttons=[("confirm_publish", "Apply"), ("cancel_publish", "Cancel")],
    )

    assert observed["url"] == "https://graph.facebook.com/v21.0/123456/messages"
    assert observed["authorization"] == "Bearer token"
    assert observed["body"] == {
        "messaging_product": "whatsapp",
        "to": "+972526508861",
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": "Publish now?"},
            "action": {
                "buttons": [
                    {
                        "type": "reply",
                        "reply": {"id": "confirm_publish", "title": "Apply"},
                    },
                    {
                        "type": "reply",
                        "reply": {"id": "cancel_publish", "title": "Cancel"},
                    },
                ]
            },
        },
    }
    await client.aclose()
