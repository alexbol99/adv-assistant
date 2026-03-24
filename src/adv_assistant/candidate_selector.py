from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlparse

from adv_assistant.product_resolution_models import ProductCandidate

_TOKEN_RE = re.compile(r"[A-Za-z0-9\u0590-\u05FF]+")

_IRRELEVANT_TERMS = {
    "recipe",
    "recipes",
    "youtube",
    "tiktok",
    "pinterest",
}
_GENERIC_PAGE_TERMS = {
    "search",
    "results",
    "catalog",
    "חיפוש",
    "מוצרים",
    "באתר",
    "אונליין",
}
_UNSAFE_SOURCE_HOSTS = {
    "tiktok.com",
    "instagram.com",
    "lookaside.instagram.com",
    "l.instagram.com",
    "facebook.com",
    "fbcdn.net",
    "youtube.com",
    "youtu.be",
    "pinterest.com",
    "x.com",
    "twitter.com",
}


def _tokenize(value: str) -> list[str]:
    return [token.casefold() for token in _TOKEN_RE.findall(value)]


def _normalize(value: str) -> str:
    return " ".join(_tokenize(value))


@dataclass(slots=True)
class CandidateSelector:
    minimum_score: float = 2.0

    def select_best(
        self,
        *,
        product_query: str,
        brand: str | None,
        candidates: list[ProductCandidate],
    ) -> ProductCandidate | None:
        if not candidates:
            return None
        query_tokens = _tokenize(product_query)
        scored = [
            (
                self._score_candidate(
                    candidate=candidate,
                    product_query=product_query,
                    brand=brand,
                ),
                candidate,
            )
            for candidate in candidates
        ]
        ranked = sorted(scored, key=lambda item: item[0], reverse=True)
        for score, candidate in ranked:
            if score < self.minimum_score:
                return None
            if not self._is_usable_candidate(candidate=candidate, query_tokens=query_tokens):
                continue
            return candidate
        return None

    def _is_usable_candidate(
        self,
        *,
        candidate: ProductCandidate,
        query_tokens: list[str],
    ) -> bool:
        if not candidate.title or not candidate.image_url:
            return False
        if _candidate_has_unsafe_source(candidate):
            return False
        if _looks_templated(candidate.title):
            return False
        if _looks_templated(candidate.image_url):
            return False
        if candidate.product_url and _looks_templated(candidate.product_url):
            return False
        if _is_generic_page_title(candidate.title, query_tokens=query_tokens):
            return False
        return True

    def _score_candidate(
        self,
        *,
        candidate: ProductCandidate,
        product_query: str,
        brand: str | None,
    ) -> float:
        title_norm = _normalize(candidate.title)
        query_tokens = _tokenize(product_query)
        title_tokens = set(_tokenize(candidate.title))
        description_tokens = set(_tokenize(candidate.description or ""))
        score = 0.0

        if title_norm:
            score += 0.8
        if candidate.image_url:
            score += 1.4
        if candidate.description:
            score += 0.2
        if candidate.product_url:
            score += 0.4

        normalized_query = _normalize(product_query)
        if normalized_query and normalized_query in title_norm:
            score += 4.0

        if query_tokens:
            matched = 0
            for token in query_tokens:
                if token in title_tokens:
                    matched += 1
                elif token in description_tokens:
                    matched += 0.5
            score += (matched / len(query_tokens)) * 3.0

        if brand:
            brand_tokens = _tokenize(brand)
            if brand_tokens and all(token in title_tokens for token in brand_tokens):
                score += 1.8

        low_title_tokens = title_tokens
        if any(token in low_title_tokens for token in _IRRELEVANT_TERMS):
            score -= 2.0

        return score


def _looks_templated(value: str) -> bool:
    lowered = value.casefold()
    return "{{" in lowered or "}}" in lowered


def _candidate_has_unsafe_source(candidate: ProductCandidate) -> bool:
    hosts = {
        _extract_host(candidate.source),
        _extract_host(candidate.product_url),
        _extract_host(candidate.image_url),
    }
    for host in hosts:
        if host is None:
            continue
        if _is_unsafe_host(host):
            return True
    return False


def _extract_host(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    host = parsed.netloc.strip().lower()
    if not host and "://" not in raw:
        host = raw.split("/")[0].strip().lower()
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _is_unsafe_host(host: str) -> bool:
    normalized = host.casefold()
    if normalized in _UNSAFE_SOURCE_HOSTS:
        return True
    return any(normalized.endswith(f".{blocked}") for blocked in _UNSAFE_SOURCE_HOSTS)


def _is_generic_page_title(title: str, *, query_tokens: list[str]) -> bool:
    title_tokens = set(_tokenize(title))
    generic_hits = title_tokens & _GENERIC_PAGE_TERMS
    if not generic_hits:
        return False

    unique_query_tokens = set(query_tokens)
    if not unique_query_tokens:
        return True
    overlap = len(title_tokens & unique_query_tokens) / max(1, len(unique_query_tokens))
    return overlap < 0.5
