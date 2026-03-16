import pytest

from adv_assistant.ai_extractor import HeuristicProductAIExtractor
from adv_assistant.candidate_selector import CandidateSelector
from adv_assistant.product_resolution_models import ExtractedProductQuery, ProductCandidate
from adv_assistant.product_resolution_service import DefaultProductResolutionService

pytestmark = pytest.mark.anyio


class StaticExtractor:
    async def extract(
        self,
        *,
        message_text: str,
        language: str,
    ) -> ExtractedProductQuery:
        return ExtractedProductQuery(
            raw_user_text=message_text,
            brand="Schweppes",
            product_query="Schweppes tonic 1.5L",
            flavor="tonic",
            volume="1.5L",
            status_hint="specific_query",
        )


class EmptyRetailerSearchService:
    async def search(self, *, product_query: str) -> list[ProductCandidate]:
        return []

    async def close(self) -> None:
        return None


class StaticSerperSearchService:
    enabled = True

    async def search(self, *, query: str, language: str) -> list[ProductCandidate]:
        return [
            ProductCandidate(
                title="Schweppes Tonic 1.5L",
                description="Sparkling soft drink",
                image_url="https://images.example/schweppes-tonic.jpg",
                product_url="https://example.com/schweppes-tonic",
                source="images.example",
                search_method="serper_fallback",
            )
        ]

    async def close(self) -> None:
        return None


async def test_heuristic_extractor_normalizes_brand_query_and_attributes() -> None:
    extractor = HeuristicProductAIExtractor()

    result = await extractor.extract(
        message_text="Make me an ad for Coca Cola Zero 1.5 liter bottle",
        language="en",
    )

    assert result.brand == "Coca Cola"
    assert result.product_query == "Coca Cola Zero 1.5 liter bottle"
    assert result.flavor == "Zero"
    assert result.volume == "1.5L"
    assert result.package_type == "bottle"
    assert result.status_hint == "specific_query"


def test_candidate_selector_prefers_relevant_candidate_with_image() -> None:
    selector = CandidateSelector()
    query = "Coca Cola Zero 1.5L"
    candidates = [
        ProductCandidate(
            title="Coca Cola Zero 1.5L",
            description="No image candidate",
            image_url=None,
            product_url="https://example.com/no-image",
            source="retailer-a",
        ),
        ProductCandidate(
            title="Coca Cola Zero 1.5L bottle",
            description="Exact candidate with image",
            image_url="https://images.example/coke-zero.jpg",
            product_url="https://example.com/with-image",
            source="retailer-b",
        ),
        ProductCandidate(
            title="Cola drink 2L",
            description="Less relevant",
            image_url="https://images.example/cola.jpg",
            product_url="https://example.com/cola",
            source="retailer-c",
        ),
    ]

    selected = selector.select_best(
        product_query=query,
        brand="Coca Cola",
        candidates=candidates,
    )

    assert selected is not None
    assert selected.title == "Coca Cola Zero 1.5L bottle"
    assert selected.source == "retailer-b"


def test_candidate_selector_skips_templated_top_result_and_chooses_next_usable() -> None:
    selector = CandidateSelector()
    query = "שישיית קולה זירו"
    candidates = [
        ProductCandidate(
            title="{{MetaTags.productTitle || MetaTags.title}}",
            description="Template title from site shell",
            image_url="https://retailer.example/template.jpg",
            product_url="{{MetaTags.productCanonical || MetaTags.canonical}}",
            source="retailer-template",
        ),
        ProductCandidate(
            title="שישיית קוקה קולה זירו 330 מ\"ל",
            description="מארז שישייה",
            image_url="https://retailer.example/coke-zero-6pack.jpg",
            product_url="https://retailer.example/products/coke-zero-6pack",
            source="retailer-valid",
        ),
    ]

    selected = selector.select_best(
        product_query=query,
        brand="קולה",
        candidates=candidates,
    )

    assert selected is not None
    assert selected.source == "retailer-valid"


def test_candidate_selector_rejects_generic_search_pages() -> None:
    selector = CandidateSelector()
    query = "שישיית קולה זירו"
    candidates = [
        ProductCandidate(
            title="חיפוש מוצרים באתר - רמי לוי אונליין",
            description="Search results page",
            image_url="https://retailer.example/logo.jpg",
            product_url="https://retailer.example/search",
            source="retailer-search-page",
        )
    ]

    selected = selector.select_best(
        product_query=query,
        brand="קולה",
        candidates=candidates,
    )

    assert selected is None


async def test_product_resolution_falls_back_to_serper_when_retailers_empty() -> None:
    service = DefaultProductResolutionService(
        ai_extractor=StaticExtractor(),
        retailer_search_service=EmptyRetailerSearchService(),
        serper_image_search_service=StaticSerperSearchService(),
        candidate_selector=CandidateSelector(),
    )

    result = await service.resolve(
        message_text="I want to make an ad for Schweppes tonic",
        language="en",
    )

    assert result.status == "resolved"
    assert result.product_query == "Schweppes tonic 1.5L"
    assert result.selected_result is not None
    assert result.selected_result.title == "Schweppes Tonic 1.5L"
    assert result.selected_result.search_method == "serper_fallback"
