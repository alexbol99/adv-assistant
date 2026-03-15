"""Product discovery: text-based product search across retailer sites and Serper fallback.

This module provides a provider-chain pattern for searching Israeli retailer
websites for product images and titles, falling back to Google Images via
Serper API when retailers don't return usable results.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


class DiscoveryStatus(StrEnum):
    RESOLVED = "resolved"
    NOT_FOUND = "not_found"


@dataclass(slots=True)
class DiscoveredProduct:
    """A single product result from discovery."""

    title: str
    image_url: str
    source: str  # "shufersal", "serper", etc.
    product_url: str | None = None
    description: str | None = None


@dataclass(slots=True)
class DiscoveryResult:
    """Overall discovery outcome."""

    status: DiscoveryStatus
    product: DiscoveredProduct | None = None
    search_method: str | None = None  # "retailer" or "serper_fallback"


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class ProductSearchProvider(Protocol):
    @property
    def source(self) -> str: ...

    async def search(self, *, query: str) -> DiscoveredProduct | None: ...

    async def close(self) -> None: ...


class ProductDiscoveryService(Protocol):
    async def discover(self, *, query: str) -> DiscoveryResult: ...

    async def close(self) -> None: ...


# ---------------------------------------------------------------------------
# Retry helper (mirrors enrichment.py)
# ---------------------------------------------------------------------------


def _retry_delay_seconds(*, attempt: int, base_seconds: float) -> float:
    safe_attempt = max(1, attempt)
    safe_base = max(0.0, base_seconds)
    return safe_base * (2 ** (safe_attempt - 1))


def _is_retryable_status(status_code: int | None) -> bool:
    if status_code is None:
        return False
    if status_code in {408, 429}:
        return True
    return 500 <= status_code <= 599


# ---------------------------------------------------------------------------
# Shufersal provider
# ---------------------------------------------------------------------------

# Regex patterns for parsing Shufersal search results HTML.
_SHUFERSAL_IMAGE_RE = re.compile(
    r"https://res\.cloudinary\.com/shufersal/image/upload/[^\s\"'<>]+",
)
# Product titles appear in data attributes or miglog-like markup.
_SHUFERSAL_PRODUCT_TITLE_RE = re.compile(
    r'(?:data-product-name|data-title|"name")\s*[:=]\s*"([^"]{2,120})"',
)
# Fallback: look for a JSON-like "ProductName" or "product_name" field.
_SHUFERSAL_JSON_TITLE_RE = re.compile(
    r'"(?:ProductName|product_name|productName)"\s*:\s*"([^"]{2,120})"',
)
# Product URLs on Shufersal follow patterns like /online/he/...
_SHUFERSAL_PRODUCT_URL_RE = re.compile(
    r'href="(/online/he/[^"]*?/p-\d+[^"]*)"',
)


class ShufersalSearchProvider:
    """Search Shufersal's public product catalog."""

    def __init__(
        self,
        *,
        base_url: str = "https://www.shufersal.co.il",
        timeout_seconds: float = 10.0,
        max_attempts: int = 2,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._max_attempts = max(1, max_attempts)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._client = client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={
                "User-Agent": "adv-assistant/0.1 (product-discovery)",
                "Accept-Language": "he-IL,he;q=0.9,en;q=0.5",
            },
            follow_redirects=True,
        )

    @property
    def source(self) -> str:
        return "shufersal"

    async def search(self, *, query: str) -> DiscoveredProduct | None:
        stripped = query.strip()
        if not stripped:
            return None

        response: httpx.Response | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(
                    "/online/he/search/results",
                    params={"q": stripped},
                )
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if _is_retryable_status(status_code) and attempt < self._max_attempts:
                    delay = _retry_delay_seconds(
                        attempt=attempt, base_seconds=self._retry_base_seconds
                    )
                    logger.warning(
                        "Shufersal search retryable HTTP error for q=%r "
                        "(status=%s, attempt=%s/%s, retry_in=%.2fs): %s",
                        stripped,
                        status_code,
                        attempt,
                        self._max_attempts,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning("Shufersal search failed for q=%r: %s", stripped, exc)
                return None
            except httpx.RequestError as exc:
                if attempt < self._max_attempts:
                    delay = _retry_delay_seconds(
                        attempt=attempt, base_seconds=self._retry_base_seconds
                    )
                    logger.warning(
                        "Shufersal search retryable request error for q=%r "
                        "(attempt=%s/%s, retry_in=%.2fs): %s",
                        stripped,
                        attempt,
                        self._max_attempts,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning("Shufersal search failed for q=%r: %s", stripped, exc)
                return None

        if response is None:
            return None

        return self._parse_response(response.text)

    def _parse_response(self, html: str) -> DiscoveredProduct | None:
        """Extract the first product with both title and image from the HTML."""
        # Try to extract product image URLs.
        images = _SHUFERSAL_IMAGE_RE.findall(html)
        if not images:
            return None

        # Pick the best image: prefer larger variants (avoid thumbnails).
        image_url = _select_best_shufersal_image(images)

        # Try to extract product title.
        title = _extract_shufersal_title(html)
        if title is None:
            return None

        # Try to extract product URL.
        product_url: str | None = None
        url_match = _SHUFERSAL_PRODUCT_URL_RE.search(html)
        if url_match:
            product_url = f"{self._base_url}{url_match.group(1)}"

        return DiscoveredProduct(
            title=title,
            image_url=image_url,
            source="shufersal",
            product_url=product_url,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _select_best_shufersal_image(urls: list[str]) -> str:
    """Pick the best Cloudinary image URL, preferring larger sizes."""
    # Prefer URLs containing size indicators for larger images.
    size_priority = ["_large", "_medium", "superZoom", "zoom", "_small"]
    for size_hint in size_priority:
        for url in urls:
            if size_hint in url:
                return url
    # Fallback: return the first image found.
    return urls[0]


def _extract_shufersal_title(html: str) -> str | None:
    """Extract a product title from Shufersal HTML."""
    match = _SHUFERSAL_PRODUCT_TITLE_RE.search(html)
    if match:
        return match.group(1).strip()
    match = _SHUFERSAL_JSON_TITLE_RE.search(html)
    if match:
        return match.group(1).strip()
    return None


# ---------------------------------------------------------------------------
# Serper (Google Images) provider
# ---------------------------------------------------------------------------

_MIN_IMAGE_WIDTH = 200
_MIN_IMAGE_HEIGHT = 200


class SerperImageSearchProvider:
    """Search Google Images via Serper API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://google.serper.dev",
        timeout_seconds: float = 8.0,
        max_attempts: int = 2,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = api_key
        self._owns_client = client is None
        self._max_attempts = max(1, max_attempts)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
        )

    @property
    def source(self) -> str:
        return "serper"

    async def search(self, *, query: str) -> DiscoveredProduct | None:
        stripped = query.strip()
        if not stripped:
            return None

        payload = {
            "q": f"{stripped} product",
            "gl": "il",
            "hl": "he",
            "num": 10,
        }
        headers = {
            "X-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }

        response: httpx.Response | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.post(
                    "/images",
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                if _is_retryable_status(status_code) and attempt < self._max_attempts:
                    delay = _retry_delay_seconds(
                        attempt=attempt, base_seconds=self._retry_base_seconds
                    )
                    logger.warning(
                        "Serper image search retryable HTTP error for q=%r "
                        "(status=%s, attempt=%s/%s, retry_in=%.2fs): %s",
                        stripped,
                        status_code,
                        attempt,
                        self._max_attempts,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning("Serper image search failed for q=%r: %s", stripped, exc)
                return None
            except httpx.RequestError as exc:
                if attempt < self._max_attempts:
                    delay = _retry_delay_seconds(
                        attempt=attempt, base_seconds=self._retry_base_seconds
                    )
                    logger.warning(
                        "Serper image search retryable request error for q=%r "
                        "(attempt=%s/%s, retry_in=%.2fs): %s",
                        stripped,
                        attempt,
                        self._max_attempts,
                        delay,
                        exc,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.warning("Serper image search failed for q=%r: %s", stripped, exc)
                return None

        if response is None:
            return None

        return self._parse_response(response.json())

    def _parse_response(self, data: dict[str, Any]) -> DiscoveredProduct | None:
        images = data.get("images")
        if not isinstance(images, list) or not images:
            return None

        for img in images:
            if not isinstance(img, dict):
                continue
            image_url = img.get("imageUrl")
            title = img.get("title")
            if not image_url or not title:
                continue
            # Filter out tiny images.
            width = img.get("imageWidth", 0)
            height = img.get("imageHeight", 0)
            if (isinstance(width, int) and width < _MIN_IMAGE_WIDTH) or (
                isinstance(height, int) and height < _MIN_IMAGE_HEIGHT
            ):
                continue
            return DiscoveredProduct(
                title=str(title),
                image_url=str(image_url),
                source="serper",
                product_url=img.get("link"),
            )
        return None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


# ---------------------------------------------------------------------------
# Provider chain
# ---------------------------------------------------------------------------


class ProviderChainDiscoveryService:
    """Try each provider in order, return first successful result."""

    def __init__(self, *, providers: list[ProductSearchProvider] | None = None) -> None:
        self._providers: list[ProductSearchProvider] = providers or []

    async def discover(self, *, query: str) -> DiscoveryResult:
        if not query.strip():
            return DiscoveryResult(status=DiscoveryStatus.NOT_FOUND)

        for provider in self._providers:
            try:
                result = await provider.search(query=query)
                if result is not None:
                    method = "serper_fallback" if provider.source == "serper" else "retailer"
                    return DiscoveryResult(
                        status=DiscoveryStatus.RESOLVED,
                        product=result,
                        search_method=method,
                    )
            except Exception:
                logger.warning(
                    "Discovery provider %s failed for q=%r",
                    provider.source,
                    query,
                    exc_info=True,
                )
                continue

        return DiscoveryResult(status=DiscoveryStatus.NOT_FOUND)

    async def close(self) -> None:
        for provider in self._providers:
            try:
                await provider.close()
            except Exception:
                logger.warning(
                    "Error closing discovery provider %s",
                    provider.source,
                    exc_info=True,
                )


class NoopProductDiscoveryService:
    """Disabled or unconfigured discovery service."""

    def __init__(self, *, reason: str = "unconfigured") -> None:
        self._reason = reason

    async def discover(self, *, query: str) -> DiscoveryResult:
        return DiscoveryResult(status=DiscoveryStatus.NOT_FOUND)

    async def close(self) -> None:
        return None
