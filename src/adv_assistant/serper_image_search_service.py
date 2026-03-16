from __future__ import annotations

import logging
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from adv_assistant.product_resolution_models import ProductCandidate

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SerperImageSearchService:
    api_key: str | None
    base_url: str = "https://google.serper.dev"
    timeout_seconds: float = 8.0
    max_results: int = 8
    client: httpx.AsyncClient | None = None
    _client: httpx.AsyncClient = field(init=False, repr=False)
    _owns_client: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._owns_client = self.client is None
        self._client = self.client or httpx.AsyncClient(
            base_url=self.base_url.rstrip("/"),
            timeout=self.timeout_seconds,
            headers={"Content-Type": "application/json"},
        )

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def search(self, *, query: str, language: str) -> list[ProductCandidate]:
        if not self.enabled:
            return []
        if not query.strip():
            return []
        try:
            response = await self._client.post(
                "/images",
                headers={"X-API-KEY": self.api_key or ""},
                json={
                    "q": query,
                    "gl": "il",
                    "hl": language.lower() if language else "he",
                    "num": self.max_results,
                },
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.RequestError as exc:
            logger.warning("Serper image fallback request failed (query=%s): %s", query, exc)
            return []
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Serper image fallback failed with status=%s (query=%s): %s",
                exc.response.status_code if exc.response is not None else None,
                query,
                exc,
            )
            return []
        except ValueError as exc:
            logger.warning("Serper image fallback invalid JSON (query=%s): %s", query, exc)
            return []

        images = payload.get("images")
        if not isinstance(images, list):
            return []

        candidates: list[ProductCandidate] = []
        for item in images:
            if not isinstance(item, dict):
                continue
            image_url = _clean_text(item.get("imageUrl")) or _clean_text(item.get("thumbnailUrl"))
            if image_url is None:
                continue
            title = _clean_text(item.get("title")) or _clean_text(item.get("source"))
            if title is None:
                continue
            product_url = _clean_text(item.get("link"))
            description = _clean_text(item.get("snippet")) or _clean_text(item.get("source"))
            source = _source_name(product_url) or "serper"
            candidates.append(
                ProductCandidate(
                    title=title,
                    description=description,
                    image_url=image_url,
                    product_url=product_url,
                    source=source,
                    search_method="serper_fallback",
                )
            )
            if len(candidates) >= self.max_results:
                break
        return candidates

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _clean_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = " ".join(value.split()).strip()
    if not stripped:
        return None
    return stripped


def _source_name(url: str | None) -> str | None:
    if not url:
        return None
    parsed = urlparse(url)
    if not parsed.netloc:
        return None
    host = parsed.netloc.lower()
    if host.startswith("www."):
        return host[4:]
    return host
