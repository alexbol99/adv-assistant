from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol

from adv_assistant.ai_extractor import ProductAIExtractor
from adv_assistant.candidate_selector import CandidateSelector
from adv_assistant.product_resolution_models import (
    ProductCandidate,
    ProductResolutionResult,
    SelectedProductResult,
)
from adv_assistant.retailer_search_service import RetailerSearchService
from adv_assistant.serper_image_search_service import SerperImageSearchService

logger = logging.getLogger(__name__)


class ProductResolutionService(Protocol):
    @property
    def enabled(self) -> bool: ...

    async def resolve(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ProductResolutionResult: ...

    async def close(self) -> None: ...


class NoopProductResolutionService:
    @property
    def enabled(self) -> bool:
        return False

    async def resolve(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ProductResolutionResult:
        return ProductResolutionResult(
            status="not_found",
            brand=None,
            product_query=message_text,
            raw_user_text=message_text,
            reason="Product resolution service disabled",
        )

    async def close(self) -> None:
        return None


@dataclass(slots=True)
class DefaultProductResolutionService:
    ai_extractor: ProductAIExtractor
    retailer_search_service: RetailerSearchService
    serper_image_search_service: SerperImageSearchService
    candidate_selector: CandidateSelector

    @property
    def enabled(self) -> bool:
        return True

    async def resolve(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ProductResolutionResult:
        extracted = await self.ai_extractor.extract(
            message_text=message_text,
            language=language,
        )
        logger.info(
            "Product query extracted "
            "(brand=%s, query=%s, flavor=%s, volume=%s, package=%s, hint=%s)",
            extracted.brand,
            extracted.product_query,
            extracted.flavor,
            extracted.volume,
            extracted.package_type,
            extracted.status_hint,
        )

        product_query = extracted.product_query.strip()
        if len(product_query) < 2:
            return ProductResolutionResult(
                status="needs_clarification",
                brand=extracted.brand,
                product_query=extracted.product_query,
                raw_user_text=extracted.raw_user_text,
                flavor=extracted.flavor,
                volume=extracted.volume,
                package_type=extracted.package_type,
                clarification_question=_clarification_question(extracted.product_query),
            )

        retailer_candidates = await self.retailer_search_service.search(product_query=product_query)
        logger.info(
            "Retailer candidates fetched (query=%s, count=%s)",
            product_query,
            len(retailer_candidates),
        )
        selected = self.candidate_selector.select_best(
            product_query=product_query,
            brand=extracted.brand,
            candidates=retailer_candidates,
        )
        if selected is not None:
            logger.info(
                "Retailer candidate selected (query=%s, source=%s, title=%s)",
                product_query,
                selected.source,
                selected.title,
            )
            return _resolved_result(extracted, selected)

        fallback_query = _fallback_query(extracted.product_query, extracted.brand)
        logger.info(
            "Retailer candidates unusable, fallback to Serper (query=%s, fallback_query=%s)",
            product_query,
            fallback_query,
        )
        fallback_candidates = await self.serper_image_search_service.search(
            query=fallback_query,
            language=language,
        )
        logger.info(
            "Serper fallback candidates fetched (query=%s, count=%s)",
            fallback_query,
            len(fallback_candidates),
        )
        selected_fallback = self.candidate_selector.select_best(
            product_query=product_query,
            brand=extracted.brand,
            candidates=fallback_candidates,
        )
        if selected_fallback is not None:
            logger.info(
                "Serper fallback candidate selected (query=%s, source=%s, title=%s)",
                fallback_query,
                selected_fallback.source,
                selected_fallback.title,
            )
            return _resolved_result(extracted, selected_fallback)

        if extracted.status_hint == "broad_query":
            return ProductResolutionResult(
                status="needs_clarification",
                brand=extracted.brand,
                product_query=extracted.product_query,
                raw_user_text=extracted.raw_user_text,
                flavor=extracted.flavor,
                volume=extracted.volume,
                package_type=extracted.package_type,
                clarification_question=_clarification_question(extracted.product_query),
            )

        return ProductResolutionResult(
            status="not_found",
            brand=extracted.brand,
            product_query=extracted.product_query,
            raw_user_text=extracted.raw_user_text,
            flavor=extracted.flavor,
            volume=extracted.volume,
            package_type=extracted.package_type,
            reason="No usable retailer or Serper image results",
        )

    async def close(self) -> None:
        await self.retailer_search_service.close()
        await self.serper_image_search_service.close()


def _resolved_result(extracted, selected: ProductCandidate) -> ProductResolutionResult:
    image_url = selected.image_url or ""
    return ProductResolutionResult(
        status="resolved",
        brand=extracted.brand,
        product_query=extracted.product_query,
        raw_user_text=extracted.raw_user_text,
        flavor=extracted.flavor,
        volume=extracted.volume,
        package_type=extracted.package_type,
        selected_result=SelectedProductResult(
            title=selected.title,
            description=selected.description,
            image_url=image_url,
            product_url=selected.product_url,
            source=selected.source,
            search_method=selected.search_method,
        ),
    )


def _fallback_query(product_query: str, brand: str | None) -> str:
    if brand and brand.casefold() not in product_query.casefold():
        return f"{brand} {product_query} Israel"
    return f"{product_query} Israel"


def _clarification_question(product_query: str) -> str:
    if not product_query.strip():
        return "Which product do you want to advertise? For example, tonic 1.5L or 500ml."
    return (
        f"Which {product_query} product do you mean? "
        "For example tonic 1.5L or 500ml."
    )
