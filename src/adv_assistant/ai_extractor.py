from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

from openai import AsyncOpenAI
from pydantic import BaseModel, Field, ValidationError

from adv_assistant.llm_gateway import sanitize_user_text
from adv_assistant.product_resolution_models import ExtractedProductQuery

logger = logging.getLogger(__name__)

_VOLUME_RE = re.compile(
    r"(?P<value>\d+(?:[.,]\d+)?)\s*(?P<unit>l|liter|litre|liters|litres|ml|מ\"ל|מל|ליטר|ל)\b",
    re.IGNORECASE,
)
_TOKEN_RE = re.compile(r"[A-Za-z0-9\u0590-\u05FF]+")
_MULTISPACE_RE = re.compile(r"\s+")
_HEBREW_CHAR_RE = re.compile(r"[\u0590-\u05FF]")
_NON_TEXT_SYMBOLS_RE = re.compile(r"[^\w\s\u0590-\u05FF.+-]")

_AD_PREFIX_PATTERNS = (
    re.compile(r"^\s*i\s+want\s+to\s+(?:make|create)\s+an?\s+ad\s+for\s+", re.IGNORECASE),
    re.compile(r"^\s*(?:make|create)\s+(?:me\s+)?an?\s+ad\s+for\s+", re.IGNORECASE),
    re.compile(r"^\s*i\s+want\s+(?:a\s+)?promotion\s+for\s+", re.IGNORECASE),
    re.compile(r"^\s*promotion\s+for\s+", re.IGNORECASE),
    re.compile(r"^\s*אני\s+רוצה\s+(?:ליצור|לעשות)\s+פרסומת\s+ל", re.IGNORECASE),
    re.compile(r"^\s*תכין\s+לי\s+פרסומת\s+ל", re.IGNORECASE),
    re.compile(r"^\s*מבצע\s+על\s+", re.IGNORECASE),
    re.compile(r"^\s*פרסומת\s+ל", re.IGNORECASE),
)

_PACKAGE_KEYWORDS = {
    "bottle": "bottle",
    "can": "can",
    "pack": "pack",
    "carton": "carton",
    "בקבוק": "bottle",
    "פחית": "can",
    "מארז": "pack",
    "קרטון": "carton",
}

_FLAVOR_KEYWORDS = {
    "zero",
    "diet",
    "tonic",
    "lemon",
    "orange",
    "grape",
    "peach",
    "mango",
    "רגיל",
    "דיאט",
    "זירו",
    "טוניק",
    "לימון",
    "תפוזים",
}

_BROAD_QUERY_TOKENS = {
    "drink",
    "beverage",
    "soda",
    "water",
    "juice",
    "משקה",
    "שתייה",
}


class ProductAIExtractor(Protocol):
    async def extract(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ExtractedProductQuery: ...


def _supports_explicit_zero_temperature(exc: Exception) -> bool:
    message = str(exc).lower()
    if "temperature" not in message:
        return True
    if "does not support 0" in message:
        return False
    if "only the default" in message and "temperature" in message:
        return False
    return True


def _normalize_whitespace(value: str) -> str:
    return _MULTISPACE_RE.sub(" ", value).strip()


def _clean_message_text(value: str) -> str:
    cleaned = _normalize_whitespace(value)
    for pattern in _AD_PREFIX_PATTERNS:
        updated = pattern.sub("", cleaned)
        if updated != cleaned:
            cleaned = _normalize_whitespace(updated)
            break
    cleaned = _NON_TEXT_SYMBOLS_RE.sub(" ", cleaned)
    return _normalize_whitespace(cleaned)


def _normalize_volume(raw_value: str | None, raw_unit: str | None) -> str | None:
    if not raw_value or not raw_unit:
        return None
    numeric = raw_value.replace(",", ".")
    unit = raw_unit.lower()
    if unit in {"l", "liter", "litre", "liters", "litres", "ליטר", "ל"}:
        return f"{numeric}L"
    return f"{numeric}ml"


def _extract_volume(value: str) -> str | None:
    match = _VOLUME_RE.search(value)
    if not match:
        return None
    return _normalize_volume(match.group("value"), match.group("unit"))


def _extract_package_type(tokens: list[str]) -> str | None:
    for token in tokens:
        normalized = token.casefold()
        package = _PACKAGE_KEYWORDS.get(normalized)
        if package:
            return package
    return None


def _extract_flavor(tokens: list[str]) -> str | None:
    for token in tokens:
        normalized = token.casefold()
        if normalized in _FLAVOR_KEYWORDS:
            return token
    return None


def _is_probably_hebrew(value: str) -> bool:
    return bool(_HEBREW_CHAR_RE.search(value))


def _title_case_brand(tokens: list[str], raw_query: str) -> str | None:
    if not tokens:
        return None
    lowered = [token.casefold() for token in tokens]
    if len(tokens) >= 2 and lowered[0] == "coca" and lowered[1] == "cola":
        return "Coca Cola"
    first = tokens[0]
    if _is_probably_hebrew(first):
        return first
    return first.title()


def _extract_brand(tokens: list[str], raw_query: str) -> str | None:
    if not tokens:
        return None
    blocked = {token for token in tokens if token.casefold() in _FLAVOR_KEYWORDS}
    volume = _extract_volume(raw_query)
    volume_tokens = set(_TOKEN_RE.findall(volume)) if volume else set()
    brand_tokens: list[str] = []
    for token in tokens:
        if token in blocked:
            break
        if token in volume_tokens:
            break
        normalized = token.casefold()
        if normalized in _PACKAGE_KEYWORDS:
            break
        brand_tokens.append(token)
        if len(brand_tokens) == 2:
            break
    return _title_case_brand(brand_tokens or [tokens[0]], raw_query)


def _status_hint(product_query: str, brand: str | None) -> str:
    tokens = [token.casefold() for token in _TOKEN_RE.findall(product_query)]
    if len(tokens) <= 1:
        return "broad_query"
    if brand is not None and _normalize_whitespace(product_query).casefold() == brand.casefold():
        return "broad_query"
    if any(token in _BROAD_QUERY_TOKENS for token in tokens):
        return "broad_query"
    return "specific_query"


class HeuristicProductAIExtractor:
    async def extract(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ExtractedProductQuery:
        sanitized = sanitize_user_text(message_text, max_chars=500)
        cleaned_query = _clean_message_text(sanitized)
        query = cleaned_query or sanitized or message_text.strip()
        tokens = _TOKEN_RE.findall(query)
        brand = _extract_brand(tokens, query)
        flavor = _extract_flavor(tokens)
        volume = _extract_volume(query)
        package_type = _extract_package_type(tokens)
        return ExtractedProductQuery(
            raw_user_text=sanitized,
            brand=brand,
            product_query=query,
            flavor=flavor,
            volume=volume,
            package_type=package_type,
            status_hint=_status_hint(query, brand),
        )


class _ExtractionPayload(BaseModel):
    brand: str | None = Field(default=None)
    product_query: str | None = Field(default=None)
    flavor: str | None = Field(default=None)
    volume: str | None = Field(default=None)
    package_type: str | None = Field(default=None)
    status_hint: str | None = Field(default=None)


@dataclass(slots=True)
class OpenAIProductAIExtractor:
    api_key: str
    model: str
    timeout_seconds: int
    max_input_chars: int = 500
    base_url: str | None = None
    fallback_extractor: ProductAIExtractor | None = None
    _client: AsyncOpenAI = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = AsyncOpenAI(api_key=self.api_key, base_url=self.base_url)
        if self.fallback_extractor is None:
            self.fallback_extractor = HeuristicProductAIExtractor()

    async def extract(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ExtractedProductQuery:
        sanitized = sanitize_user_text(message_text, max_chars=self.max_input_chars)
        system_prompt = (
            "You extract a product query from WhatsApp ad requests. "
            "Return strict JSON with keys: brand, product_query, flavor, volume, package_type, "
            "status_hint. status_hint must be 'specific_query' or 'broad_query'. "
            "Use null for unknown fields. Do not invent precise SKUs."
        )
        user_prompt = f"Language: {language}\nMessage: {sanitized}"
        try:
            response_text = await self._chat_json(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
            )
            payload = _ExtractionPayload.model_validate(json.loads(response_text))
            normalized = _normalize_llm_extraction(payload, raw_user_text=sanitized)
            return normalized
        except (json.JSONDecodeError, ValidationError, RuntimeError) as exc:
            logger.warning("Product extraction fallback to heuristic parser: %s", exc)
            return await self.fallback_extractor.extract(
                message_text=message_text,
                language=language,
            )

    async def _chat_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        request_kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "timeout": self.timeout_seconds,
        }
        try:
            response = await self._client.chat.completions.create(
                temperature=0,
                **request_kwargs,
            )
        except Exception as exc:
            if _supports_explicit_zero_temperature(exc):
                raise RuntimeError(f"OpenAI extraction call failed: {exc}") from exc
            response = await self._client.chat.completions.create(**request_kwargs)
        content = response.choices[0].message.content if response.choices else None
        if not content:
            raise RuntimeError("OpenAI extraction returned an empty response")
        return content


def _normalize_optional_text(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = _normalize_whitespace(value)
    if not normalized:
        return None
    return normalized


def _normalize_llm_extraction(
    payload: _ExtractionPayload,
    *,
    raw_user_text: str,
) -> ExtractedProductQuery:
    product_query = _normalize_optional_text(payload.product_query) or _clean_message_text(
        raw_user_text
    )
    if not product_query:
        product_query = raw_user_text
    brand = _normalize_optional_text(payload.brand)
    flavor = _normalize_optional_text(payload.flavor)
    volume = _normalize_optional_text(payload.volume)
    package_type = _normalize_optional_text(payload.package_type)
    status_hint = (payload.status_hint or "").strip().lower()
    if status_hint not in {"specific_query", "broad_query"}:
        status_hint = _status_hint(product_query, brand)
    return ExtractedProductQuery(
        raw_user_text=raw_user_text,
        brand=brand,
        product_query=product_query,
        flavor=flavor,
        volume=volume,
        package_type=package_type,
        status_hint=status_hint,
    )
