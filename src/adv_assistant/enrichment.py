from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Protocol

import httpx

_EAN_RE = re.compile(r"\b(\d{8,14})\b")
logger = logging.getLogger(__name__)


def normalize_ean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped.isdigit():
        return None
    if len(stripped) < 8 or len(stripped) > 14:
        return None
    return stripped


def extract_ean_from_text(value: str | None) -> str | None:
    if not value:
        return None
    match = _EAN_RE.search(value)
    if not match:
        return None
    return normalize_ean(match.group(1))


@dataclass(slots=True)
class EnrichedProduct:
    product_name: str | None = None
    brand: str | None = None
    category: str | None = None
    description: str | None = None
    image_url: str | None = None
    source: str = "none"

    def is_empty(self) -> bool:
        return (
            self.product_name is None
            and self.brand is None
            and self.category is None
            and self.description is None
            and self.image_url is None
        )


class BarcodeDecoder(Protocol):
    async def decode_ean(self, image_bytes: bytes) -> str | None: ...


class ProductLookupProvider(Protocol):
    @property
    def source(self) -> str: ...

    async def lookup(self, *, ean: str, language: str) -> EnrichedProduct | None: ...

    async def close(self) -> None: ...


class EnrichmentService(Protocol):
    async def decode_ean_from_image(self, image_bytes: bytes) -> str | None: ...

    async def enrich_by_ean(self, *, ean: str, language: str) -> EnrichedProduct | None: ...

    async def close(self) -> None: ...


class NoopBarcodeDecoder:
    async def decode_ean(self, image_bytes: bytes) -> str | None:
        return None


class BarcodeDecoderChain:
    def __init__(self, *, primary: BarcodeDecoder, fallback: BarcodeDecoder) -> None:
        self._primary = primary
        self._fallback = fallback

    async def decode_ean(self, image_bytes: bytes) -> str | None:
        primary_result = await self._primary.decode_ean(image_bytes)
        normalized_primary = normalize_ean(primary_result)
        if normalized_primary:
            return normalized_primary
        fallback_result = await self._fallback.decode_ean(image_bytes)
        return normalize_ean(fallback_result)


class NoopProductLookupProvider:
    def __init__(self, source: str) -> None:
        self._source = source

    @property
    def source(self) -> str:
        return self._source

    async def lookup(self, *, ean: str, language: str) -> EnrichedProduct | None:
        return None

    async def close(self) -> None:
        return None


class OpenFoodFactsProvider:
    def __init__(
        self,
        *,
        base_url: str = "https://world.openfoodfacts.org",
        timeout_seconds: float = 8.0,
        max_attempts: int = 2,
        retry_base_seconds: float = 0.5,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._owns_client = client is None
        self._max_attempts = max(1, max_attempts)
        self._retry_base_seconds = max(0.0, retry_base_seconds)
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
            headers={
                "User-Agent": "adv-assistant/0.1 (phase5-enrichment)",
            },
        )

    @property
    def source(self) -> str:
        return "open_food_facts"

    async def lookup(self, *, ean: str, language: str) -> EnrichedProduct | None:
        response: httpx.Response | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                response = await self._client.get(f"/api/v2/product/{ean}")
                response.raise_for_status()
                break
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code if exc.response is not None else None
                retryable = _is_retryable_open_food_facts_status(status_code)
                if retryable and attempt < self._max_attempts:
                    delay_seconds = _retry_delay_seconds(
                        attempt=attempt,
                        base_seconds=self._retry_base_seconds,
                    )
                    logger.warning(
                        "Open Food Facts lookup retryable HTTP error for ean=%s "
                        "(status=%s, attempt=%s/%s, retry_in=%.2fs): %s",
                        ean,
                        status_code,
                        attempt,
                        self._max_attempts,
                        delay_seconds,
                        exc,
                    )
                    await asyncio.sleep(delay_seconds)
                    continue
                logger.warning("Open Food Facts lookup failed for ean=%s: %s", ean, exc)
                return None
            except httpx.RequestError as exc:
                if attempt < self._max_attempts:
                    delay_seconds = _retry_delay_seconds(
                        attempt=attempt,
                        base_seconds=self._retry_base_seconds,
                    )
                    logger.warning(
                        "Open Food Facts lookup retryable request error for ean=%s "
                        "(attempt=%s/%s, retry_in=%.2fs): %s",
                        ean,
                        attempt,
                        self._max_attempts,
                        delay_seconds,
                        exc,
                    )
                    await asyncio.sleep(delay_seconds)
                    continue
                logger.warning("Open Food Facts lookup failed for ean=%s: %s", ean, exc)
                return None

        if response is None:
            return None
        payload = response.json()
        if int(payload.get("status", 0)) != 1:
            return None

        product = payload.get("product")
        if not isinstance(product, dict):
            return None

        lang = (language or "en").lower()
        product_name = _pick_text(
            product,
            f"product_name_{lang}",
            "product_name",
            "product_name_en",
        )
        brand_value = _pick_text(product, "brands")
        brand = None
        if brand_value:
            brand = brand_value.split(",")[0].strip() or None

        enriched = EnrichedProduct(
            product_name=product_name,
            brand=brand,
            category=_pick_text(product, "categories"),
            description=_pick_text(product, f"generic_name_{lang}", "generic_name"),
            image_url=_pick_text(product, "image_front_url", "image_url"),
            source=self.source,
        )
        if enriched.is_empty():
            return None
        return enriched

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class ProviderChainEnrichmentService:
    def __init__(
        self,
        *,
        barcode_decoder: BarcodeDecoder | None = None,
        providers: list[ProductLookupProvider] | None = None,
    ) -> None:
        self._barcode_decoder = barcode_decoder or BarcodeDecoderChain(
            primary=NoopBarcodeDecoder(),
            fallback=NoopBarcodeDecoder(),
        )
        self._providers = providers or []

    async def decode_ean_from_image(self, image_bytes: bytes) -> str | None:
        return await self._barcode_decoder.decode_ean(image_bytes)

    async def enrich_by_ean(self, *, ean: str, language: str) -> EnrichedProduct | None:
        normalized = normalize_ean(ean)
        if normalized is None:
            return None
        for provider in self._providers:
            result = await provider.lookup(ean=normalized, language=language)
            if result is None:
                continue
            if result.is_empty():
                continue
            return result
        return None

    async def close(self) -> None:
        for provider in self._providers:
            await provider.close()


class NoopEnrichmentService:
    async def decode_ean_from_image(self, image_bytes: bytes) -> str | None:
        return None

    async def enrich_by_ean(self, *, ean: str, language: str) -> EnrichedProduct | None:
        return None

    async def close(self) -> None:
        return None


def _pick_text(payload: dict[str, object], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if not isinstance(value, str):
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return None


def _is_retryable_open_food_facts_status(status_code: int | None) -> bool:
    if status_code is None:
        return False
    if status_code in {408, 429}:
        return True
    return 500 <= status_code <= 599


def _retry_delay_seconds(*, attempt: int, base_seconds: float) -> float:
    safe_attempt = max(1, attempt)
    safe_base = max(0.0, base_seconds)
    return safe_base * (2 ** (safe_attempt - 1))
