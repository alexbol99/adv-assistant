import base64
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from adv_assistant.config import Settings
from adv_assistant.db.base import Base
from adv_assistant.db.repositories import OperatorRepository
from adv_assistant.db.session import session_scope
from adv_assistant.main import create_app

pytestmark = pytest.mark.anyio


def _basic_auth_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return {"Authorization": f"Basic {token}"}


@pytest.fixture()
async def admin_app(tmp_path: Path):
    db_path = tmp_path / "admin_mvp.db"
    settings = Settings(
        app_name="adv-assistant-admin-test",
        database_url=f"sqlite+aiosqlite:///{db_path}",
        tasks_mode="inline",
        admin_basic_username="admin",
        admin_basic_password="secret",
    )
    app = create_app(settings)
    async with app.state.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await app.router.startup()
    yield app
    await app.router.shutdown()


@pytest.fixture()
async def admin_client(admin_app) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=admin_app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client


async def _seed_operator(app, *, phone: str, **fields) -> None:
    async with session_scope(app.state.session_factory) as session:
        await OperatorRepository(session).create(phone=phone, active=True, **fields)


async def test_admin_endpoints_require_basic_auth(admin_client: AsyncClient) -> None:
    response_home = await admin_client.get("/admin")
    assert response_home.status_code == 401

    response_connect = await admin_client.post(
        "/admin/operators/connect",
        json={
            "phone": "+972500000900",
            "cms_campaign_id": 157,
            "cms_playlist_id": 139,
        },
    )
    assert response_connect.status_code == 401


async def test_admin_can_create_operator_mapping_and_fetch_it(admin_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    connect = await admin_client.post(
        "/admin/operators/connect",
        headers=headers,
        json={
            "phone": "972500000901",
            "meta_user_id": "meta-901",
            "cms_campaign_id": 157,
            "cms_playlist_id": 139,
            "active": True,
        },
    )
    assert connect.status_code == 200
    payload = connect.json()
    assert payload["phone"] == "+972500000901"
    assert payload["meta_user_id"] == "meta-901"
    assert payload["cms_campaign_id"] == 157
    assert payload["cms_playlist_id"] == 139
    assert payload["active"] is True

    by_phone = await admin_client.get(
        "/admin/operators/by-phone/+972500000901",
        headers=headers,
    )
    assert by_phone.status_code == 200
    assert by_phone.json()["meta_user_id"] == "meta-901"

    by_meta = await admin_client.get(
        "/admin/operators/by-meta/meta-901",
        headers=headers,
    )
    assert by_meta.status_code == 200
    assert by_meta.json()["phone"] == "+972500000901"


async def test_admin_can_update_existing_operator_mapping(
    admin_app,
    admin_client: AsyncClient,
) -> None:
    await _seed_operator(admin_app, phone="+972500000902")
    headers = _basic_auth_header("admin", "secret")

    first_update = await admin_client.post(
        "/admin/operators/connect",
        headers=headers,
        json={
            "phone": "+972500000902",
            "meta_user_id": "meta-902",
            "cms_campaign_id": 111,
            "cms_playlist_id": 222,
            "active": True,
        },
    )
    assert first_update.status_code == 200

    second_update = await admin_client.post(
        "/admin/operators/connect",
        headers=headers,
        json={
            "phone": "+972500000902",
            "cms_campaign_id": 333,
            "cms_playlist_id": 444,
            "active": False,
        },
    )
    assert second_update.status_code == 200
    payload = second_update.json()
    assert payload["meta_user_id"] == "meta-902"
    assert payload["cms_campaign_id"] == 333
    assert payload["cms_playlist_id"] == 444
    assert payload["active"] is False


async def test_admin_rejects_missing_identifiers(admin_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await admin_client.post(
        "/admin/operators/connect",
        headers=headers,
        json={
            "cms_campaign_id": 157,
            "cms_playlist_id": 139,
        },
    )

    assert response.status_code == 422


async def test_admin_home_renders_form(admin_client: AsyncClient) -> None:
    headers = _basic_auth_header("admin", "secret")
    response = await admin_client.get("/admin", headers=headers)

    assert response.status_code == 200
    assert "Operator CMS Mapping" in response.text
    assert "/admin/operators/connect" in response.text
    assert "Operator Profile Lookup" in response.text


async def test_admin_lookup_returns_operator_system_memory_fields(
    admin_app,
    admin_client: AsyncClient,
) -> None:
    await _seed_operator(
        admin_app,
        phone="+972500000903",
        language="en",
        currency="USD",
        business_name="My Grocery",
        logo_url="https://example.com/logo.png",
        brand_colors=["#112233", "#AABBCC"],
        store_type="grocery",
        creative_guidance="Clean layout with clear pricing",
    )
    headers = _basic_auth_header("admin", "secret")

    response = await admin_client.get(
        "/admin/operators/by-phone/+972500000903",
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["phone"] == "+972500000903"
    assert payload["language"] == "en"
    assert payload["currency"] == "USD"
    assert payload["business_name"] == "My Grocery"
    assert payload["logo_url"] == "https://example.com/logo.png"
    assert payload["brand_colors"] == ["#112233", "#AABBCC"]
    assert payload["store_type"] == "grocery"
    assert payload["creative_guidance"] == "Clean layout with clear pricing"
