from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any, Protocol
from urllib.parse import quote_plus

import httpx

from adv_assistant.product_resolution_models import ProductCandidate

logger = logging.getLogger(__name__)

_JSON_LD_RE = re.compile(
    r"<script[^>]+type=[\"']application/ld\+json[\"'][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_OG_META_RE = re.compile(
    r"<meta[^>]+property=[\"'](?P<property>og:[^\"']+)[\"'][^>]+content=[\"'](?P<content>[^\"']+)[\"']",
    re.IGNORECASE,
)


def _strip_html(value: str) -> str:
    without_tags = _TAG_RE.sub(" ", value)
    normalized = _SPACE_RE.sub(" ", unescape(without_tags)).strip()
    return normalized


def _normalize_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _SPACE_RE.sub(" ", unescape(value)).strip()
    if not normalized:
        return None
    return normalized


def _extract_image(value: Any) -> str | None:
    if isinstance(value, str):
        return _normalize_text(value)
    if isinstance(value, list):
        for item in value:
            image = _extract_image(item)
            if image:
                return image
    if isinstance(value, dict):
        image_url = value.get("url") or value.get("contentUrl")
        if isinstance(image_url, str):
            return _normalize_text(image_url)
    return None


def _json_ld_to_candidate(
    payload: dict[str, Any],
    *,
    source: str,
    search_method: str,
) -> ProductCandidate | None:
    raw_type = payload.get("@type")
    payload_type = raw_type.lower() if isinstance(raw_type, str) else ""
    if payload_type not in {"product", "listitem"}:
        return None
    if payload_type == "listitem":
        item = payload.get("item")
        if isinstance(item, dict):
            return _json_ld_to_candidate(item, source=source, search_method=search_method)
        return None
    title = _normalize_text(payload.get("name") if isinstance(payload.get("name"), str) else None)
    description = _normalize_text(
        payload.get("description") if isinstance(payload.get("description"), str) else None
    )
    product_url = _normalize_text(
        payload.get("url") if isinstance(payload.get("url"), str) else None
    )
    image_url = _extract_image(payload.get("image"))
    if title is None:
        return None
    return ProductCandidate(
        title=title,
        description=description,
        image_url=image_url,
        product_url=product_url,
        source=source,
        search_method=search_method,  # type: ignore[arg-type]
    )


def _extract_candidates_from_json_ld(
    *,
    html: str,
    source: str,
    search_method: str,
) -> list[ProductCandidate]:
    candidates: list[ProductCandidate] = []
    for match in _JSON_LD_RE.finditer(html):
        raw_block = match.group(1).strip()
        if not raw_block:
            continue
        try:
            payload = json.loads(raw_block)
        except json.JSONDecodeError:
            continue
        for node in _flatten_json_ld_nodes(payload):
            candidate = _json_ld_to_candidate(
                node,
                source=source,
                search_method=search_method,
            )
            if candidate is not None:
                candidates.append(candidate)
    return candidates


def _flatten_json_ld_nodes(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        nodes: list[dict[str, Any]] = []
        for item in payload:
            nodes.extend(_flatten_json_ld_nodes(item))
        return nodes
    if isinstance(payload, dict):
        if "@graph" in payload and isinstance(payload["@graph"], list):
            return _flatten_json_ld_nodes(payload["@graph"])
        return [payload]
    return []


def _extract_og_candidate(
    *,
    html: str,
    source: str,
    search_method: str,
) -> ProductCandidate | None:
    og_map: dict[str, str] = {}
    for match in _OG_META_RE.finditer(html):
        property_name = match.group("property").lower()
        content = _normalize_text(match.group("content"))
        if content:
            og_map[property_name] = content
    title = og_map.get("og:title")
    image_url = og_map.get("og:image")
    product_url = og_map.get("og:url")
    description = og_map.get("og:description")
    if title is None:
        return None
    return ProductCandidate(
        title=title,
        description=description,
        image_url=image_url,
        product_url=product_url,
        source=source,
        search_method=search_method,  # type: ignore[arg-type]
    )


def _dedupe_candidates(candidates: list[ProductCandidate], limit: int) -> list[ProductCandidate]:
    unique: list[ProductCandidate] = []
    seen: set[tuple[str, str]] = set()
    for candidate in candidates:
        key = (candidate.title.casefold(), (candidate.product_url or "").casefold())
        if key in seen:
            continue
        seen.add(key)
        unique.append(candidate)
        if len(unique) >= limit:
            break
    return unique


class RetailerAdapter(Protocol):
    @property
    def source(self) -> str: ...

    async def search(self, *, query: str) -> list[ProductCandidate]: ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class GenericRetailerAdapter:
    source: str
    search_url_templates: tuple[str, ...]
    timeout_seconds: float = 6.0
    max_results: int = 6
    user_agent: str = "adv-assistant/0.1 (retailer-search-mvp)"
    client: httpx.AsyncClient | None = None
    _client: httpx.AsyncClient = field(init=False, repr=False)
    _owns_client: bool = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._owns_client = self.client is None
        self._client = self.client or httpx.AsyncClient(
            timeout=self.timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": self.user_agent},
        )

    async def search(self, *, query: str) -> list[ProductCandidate]:
        if not query.strip():
            return []
        candidates: list[ProductCandidate] = []
        encoded_query = quote_plus(query.strip())
        for template in self.search_url_templates:
            url = template.format(query=encoded_query)
            try:
                response = await self._client.get(url)
                if response.status_code >= 400:
                    logger.info(
                        "Retailer search skipped non-success response "
                        "(source=%s, status=%s, url=%s)",
                        self.source,
                        response.status_code,
                        url,
                    )
                    continue
                html = response.text
                candidates.extend(
                    _extract_candidates_from_json_ld(
                        html=html,
                        source=self.source,
                        search_method="retailer",
                    )
                )
                og_candidate = _extract_og_candidate(
                    html=html,
                    source=self.source,
                    search_method="retailer",
                )
                if og_candidate is not None:
                    candidates.append(og_candidate)
                deduped = _dedupe_candidates(candidates, self.max_results)
                if deduped:
                    return deduped
            except httpx.RequestError as exc:
                logger.warning(
                    "Retailer search request failed (source=%s, query=%s, url=%s): %s",
                    self.source,
                    query,
                    url,
                    exc,
                )
                continue
            except Exception:
                logger.exception(
                    "Unexpected retailer search error (source=%s, query=%s, url=%s)",
                    self.source,
                    query,
                    url,
                )
                continue
        return _dedupe_candidates(candidates, self.max_results)

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
