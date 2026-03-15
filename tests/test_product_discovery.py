"""Tests for product_discovery module: providers, chain, and LLM extraction."""

from __future__ import annotations

import httpx
import pytest

from adv_assistant.llm_gateway import ExtractedProductQuery
from adv_assistant.product_discovery import (
    DiscoveredProduct,
    DiscoveryStatus,
    NoopProductDiscoveryService,
    ProviderChainDiscoveryService,
    SerperImageSearchProvider,
    ShufersalSearchProvider,
)

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# ExtractedProductQuery tests
# ---------------------------------------------------------------------------


def test_product_query_to_search_query_combines_fields() -> None:
    query = ExtractedProductQuery(
        brand="Schweppes",
        product_query="tonic water",
        flavor="lemon",
        volume="1.5L",
    )
    assert query.to_search_query() == "Schweppes tonic water lemon 1.5L"


def test_product_query_to_search_query_handles_none_fields() -> None:
    query = ExtractedProductQuery(brand="Coca Cola")
    assert query.to_search_query() == "Coca Cola"


def test_product_query_to_search_query_empty_when_all_none() -> None:
    query = ExtractedProductQuery()
    assert query.to_search_query() == ""


# ---------------------------------------------------------------------------
# Shufersal provider tests
# ---------------------------------------------------------------------------

_SHUFERSAL_IMG = (
    "https://res.cloudinary.com/shufersal/image/upload"
    "/f_auto,q_auto/v1234/prod/product_images/products_large/P_123456.png"
)
_SHUFERSAL_HTML_WITH_PRODUCT = (
    "<html><body>"
    '<div class="product-card" '
    'data-product-name="Schweppes Tonic Water 1.5L">'
    '<a href="/online/he/A/A12/p-123456">'
    f'<img src="{_SHUFERSAL_IMG}" />'
    "</a></div></body></html>"
)

_SHUFERSAL_IMG_2 = (
    "https://res.cloudinary.com/shufersal/image/upload"
    "/f_auto/v1/prod/product_images/products_medium/P_789.png"
)
_SHUFERSAL_HTML_JSON_TITLE = (
    "<html><script>"
    'var products = [{"ProductName": "Sprite Zero 1.5L", "code": "789"}];'
    f'</script><img src="{_SHUFERSAL_IMG_2}" /></html>'
)

_SHUFERSAL_HTML_NO_PRODUCTS = """
<html>
<body>
<div>No results found</div>
</body>
</html>
"""


async def test_shufersal_search_returns_product_on_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/online/he/search/results"
        assert "q=schweppes" in str(request.url)
        return httpx.Response(status_code=200, text=_SHUFERSAL_HTML_WITH_PRODUCT)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.shufersal.co.il",
    )
    provider = ShufersalSearchProvider(client=client)
    result = await provider.search(query="schweppes")

    assert result is not None
    assert result.source == "shufersal"
    assert result.title == "Schweppes Tonic Water 1.5L"
    assert "cloudinary.com/shufersal" in result.image_url
    await client.aclose()


async def test_shufersal_search_extracts_json_title() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, text=_SHUFERSAL_HTML_JSON_TITLE)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.shufersal.co.il",
    )
    provider = ShufersalSearchProvider(client=client)
    result = await provider.search(query="sprite")

    assert result is not None
    assert result.title == "Sprite Zero 1.5L"
    assert "cloudinary.com/shufersal" in result.image_url
    await client.aclose()


async def test_shufersal_search_returns_none_on_empty_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, text=_SHUFERSAL_HTML_NO_PRODUCTS)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.shufersal.co.il",
    )
    provider = ShufersalSearchProvider(client=client)
    result = await provider.search(query="xyznonexistent")

    assert result is None
    await client.aclose()


async def test_shufersal_search_returns_none_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=503, text="Service Unavailable")

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.shufersal.co.il",
    )
    provider = ShufersalSearchProvider(client=client)
    result = await provider.search(query="coca cola")

    assert result is None
    await client.aclose()


async def test_shufersal_search_returns_none_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network down", request=request)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.shufersal.co.il",
    )
    provider = ShufersalSearchProvider(client=client)
    result = await provider.search(query="coca cola")

    assert result is None
    await client.aclose()


async def test_shufersal_search_retries_on_503_then_succeeds() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(status_code=503, text="temporary")
        return httpx.Response(status_code=200, text=_SHUFERSAL_HTML_WITH_PRODUCT)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.shufersal.co.il",
    )
    provider = ShufersalSearchProvider(client=client, max_attempts=2, retry_base_seconds=0.0)
    result = await provider.search(query="schweppes")

    assert result is not None
    assert call_count == 2
    await client.aclose()


async def test_shufersal_search_returns_none_for_empty_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, text=_SHUFERSAL_HTML_WITH_PRODUCT)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://www.shufersal.co.il",
    )
    provider = ShufersalSearchProvider(client=client)
    result = await provider.search(query="   ")

    assert result is None
    await client.aclose()


# ---------------------------------------------------------------------------
# Serper provider tests
# ---------------------------------------------------------------------------

_SERPER_RESPONSE_WITH_IMAGES = {
    "images": [
        {
            "title": "Schweppes Tonic Water",
            "imageUrl": "https://example.com/schweppes-large.jpg",
            "imageWidth": 800,
            "imageHeight": 600,
            "thumbnailUrl": "https://example.com/schweppes-thumb.jpg",
            "source": "some-store.co.il",
            "domain": "some-store.co.il",
            "link": "https://some-store.co.il/products/schweppes",
            "position": 1,
        },
        {
            "title": "Schweppes Soda Water",
            "imageUrl": "https://example.com/schweppes2.jpg",
            "imageWidth": 500,
            "imageHeight": 400,
            "link": "https://other-store.co.il/products/schweppes2",
            "position": 2,
        },
    ]
}

_SERPER_RESPONSE_TINY_IMAGES = {
    "images": [
        {
            "title": "Tiny icon",
            "imageUrl": "https://example.com/tiny.jpg",
            "imageWidth": 50,
            "imageHeight": 50,
            "link": "https://example.com/tiny",
            "position": 1,
        },
        {
            "title": "Good image",
            "imageUrl": "https://example.com/good.jpg",
            "imageWidth": 400,
            "imageHeight": 300,
            "link": "https://example.com/good",
            "position": 2,
        },
    ]
}


async def test_serper_search_returns_first_adequate_image() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/images"
        assert request.headers["X-API-KEY"] == "test-key"
        return httpx.Response(status_code=200, json=_SERPER_RESPONSE_WITH_IMAGES)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://google.serper.dev",
    )
    provider = SerperImageSearchProvider(api_key="test-key", client=client)
    result = await provider.search(query="schweppes")

    assert result is not None
    assert result.source == "serper"
    assert result.title == "Schweppes Tonic Water"
    assert result.image_url == "https://example.com/schweppes-large.jpg"
    assert result.product_url == "https://some-store.co.il/products/schweppes"
    await client.aclose()


async def test_serper_search_skips_tiny_images() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json=_SERPER_RESPONSE_TINY_IMAGES)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://google.serper.dev",
    )
    provider = SerperImageSearchProvider(api_key="test-key", client=client)
    result = await provider.search(query="something")

    assert result is not None
    assert result.title == "Good image"
    assert result.image_url == "https://example.com/good.jpg"
    await client.aclose()


async def test_serper_search_returns_none_on_empty_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json={"images": []})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://google.serper.dev",
    )
    provider = SerperImageSearchProvider(api_key="test-key", client=client)
    result = await provider.search(query="xyz")

    assert result is None
    await client.aclose()


async def test_serper_search_returns_none_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=401, json={"error": "invalid api key"})

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://google.serper.dev",
    )
    provider = SerperImageSearchProvider(api_key="bad-key", client=client)
    result = await provider.search(query="coca cola")

    assert result is None
    await client.aclose()


async def test_serper_search_retries_on_429_then_succeeds() -> None:
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return httpx.Response(status_code=429, json={"error": "rate limited"})
        return httpx.Response(status_code=200, json=_SERPER_RESPONSE_WITH_IMAGES)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://google.serper.dev",
    )
    provider = SerperImageSearchProvider(
        api_key="test-key", client=client, max_attempts=2, retry_base_seconds=0.0
    )
    result = await provider.search(query="schweppes")

    assert result is not None
    assert call_count == 2
    await client.aclose()


async def test_serper_search_returns_none_for_empty_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=200, json=_SERPER_RESPONSE_WITH_IMAGES)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://google.serper.dev",
    )
    provider = SerperImageSearchProvider(api_key="test-key", client=client)
    result = await provider.search(query="")

    assert result is None
    await client.aclose()


# ---------------------------------------------------------------------------
# Provider chain tests
# ---------------------------------------------------------------------------


class StaticProvider:
    def __init__(
        self,
        source_name: str,
        result: DiscoveredProduct | None = None,
    ) -> None:
        self._source_name = source_name
        self._result = result
        self.calls = 0

    @property
    def source(self) -> str:
        return self._source_name

    async def search(self, *, query: str) -> DiscoveredProduct | None:
        self.calls += 1
        return self._result

    async def close(self) -> None:
        return None


class FailingProvider:
    def __init__(self, source_name: str) -> None:
        self._source_name = source_name
        self.calls = 0

    @property
    def source(self) -> str:
        return self._source_name

    async def search(self, *, query: str) -> DiscoveredProduct | None:
        self.calls += 1
        raise RuntimeError("provider crashed")

    async def close(self) -> None:
        return None


async def test_chain_returns_first_provider_success() -> None:
    product = DiscoveredProduct(
        title="Coke Zero", image_url="https://example.com/coke.jpg", source="shufersal"
    )
    shufersal = StaticProvider("shufersal", result=product)
    serper = StaticProvider("serper")
    chain = ProviderChainDiscoveryService(providers=[shufersal, serper])

    result = await chain.discover(query="coca cola")

    assert result.status == DiscoveryStatus.RESOLVED
    assert result.product is not None
    assert result.product.title == "Coke Zero"
    assert result.search_method == "retailer"
    assert shufersal.calls == 1
    assert serper.calls == 0


async def test_chain_falls_through_to_serper() -> None:
    serper_product = DiscoveredProduct(
        title="Coca Cola",
        image_url="https://example.com/coca-cola-serper.jpg",
        source="serper",
    )
    shufersal = StaticProvider("shufersal", result=None)
    serper = StaticProvider("serper", result=serper_product)
    chain = ProviderChainDiscoveryService(providers=[shufersal, serper])

    result = await chain.discover(query="coca cola")

    assert result.status == DiscoveryStatus.RESOLVED
    assert result.product is not None
    assert result.search_method == "serper_fallback"
    assert shufersal.calls == 1
    assert serper.calls == 1


async def test_chain_returns_not_found_when_all_fail() -> None:
    shufersal = StaticProvider("shufersal", result=None)
    serper = StaticProvider("serper", result=None)
    chain = ProviderChainDiscoveryService(providers=[shufersal, serper])

    result = await chain.discover(query="nonexistent product xyz")

    assert result.status == DiscoveryStatus.NOT_FOUND
    assert result.product is None
    assert shufersal.calls == 1
    assert serper.calls == 1


async def test_chain_handles_provider_exception_gracefully() -> None:
    serper_product = DiscoveredProduct(
        title="Fallback Product",
        image_url="https://example.com/fallback.jpg",
        source="serper",
    )
    failing = FailingProvider("shufersal")
    serper = StaticProvider("serper", result=serper_product)
    chain = ProviderChainDiscoveryService(providers=[failing, serper])

    result = await chain.discover(query="coca cola")

    assert result.status == DiscoveryStatus.RESOLVED
    assert result.product is not None
    assert result.product.title == "Fallback Product"
    assert failing.calls == 1
    assert serper.calls == 1


async def test_chain_returns_not_found_for_empty_query() -> None:
    provider = StaticProvider(
        "shufersal",
        result=DiscoveredProduct(
            title="Should not match", image_url="https://x.com/y.jpg", source="test"
        ),
    )
    chain = ProviderChainDiscoveryService(providers=[provider])

    result = await chain.discover(query="   ")

    assert result.status == DiscoveryStatus.NOT_FOUND
    assert provider.calls == 0


async def test_noop_service_returns_not_found() -> None:
    service = NoopProductDiscoveryService()
    result = await service.discover(query="anything")

    assert result.status == DiscoveryStatus.NOT_FOUND
    assert result.product is None


# ---------------------------------------------------------------------------
# main.py builder tests
# ---------------------------------------------------------------------------


def test_build_product_discovery_service_disabled() -> None:
    from adv_assistant.config import Settings
    from adv_assistant.main import _build_product_discovery_service
    from adv_assistant.product_discovery import NoopProductDiscoveryService

    settings = Settings(discovery_enabled=False)
    service = _build_product_discovery_service(settings)

    assert isinstance(service, NoopProductDiscoveryService)


def test_build_product_discovery_service_enabled_shufersal_only() -> None:
    from adv_assistant.config import Settings
    from adv_assistant.main import _build_product_discovery_service
    from adv_assistant.product_discovery import ProviderChainDiscoveryService

    settings = Settings(discovery_enabled=True, serper_api_key=None)
    service = _build_product_discovery_service(settings)

    assert isinstance(service, ProviderChainDiscoveryService)
    assert len(service._providers) == 1
    assert service._providers[0].source == "shufersal"


def test_build_product_discovery_service_full_chain() -> None:
    from adv_assistant.config import Settings
    from adv_assistant.main import _build_product_discovery_service
    from adv_assistant.product_discovery import ProviderChainDiscoveryService

    settings = Settings(discovery_enabled=True, serper_api_key="sk-test-123")
    service = _build_product_discovery_service(settings)

    assert isinstance(service, ProviderChainDiscoveryService)
    assert len(service._providers) == 2
    assert [p.source for p in service._providers] == ["shufersal", "serper"]
