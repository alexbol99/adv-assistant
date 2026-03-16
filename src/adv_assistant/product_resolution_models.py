from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ResolutionStatus = Literal["resolved", "needs_clarification", "not_found"]
SearchMethod = Literal["retailer", "serper_fallback"]
StatusHint = Literal["specific_query", "broad_query"]


@dataclass(slots=True)
class ExtractedProductQuery:
    raw_user_text: str
    product_query: str
    brand: str | None = None
    flavor: str | None = None
    volume: str | None = None
    package_type: str | None = None
    status_hint: StatusHint = "specific_query"


@dataclass(slots=True)
class ProductCandidate:
    title: str
    description: str | None
    image_url: str | None
    product_url: str | None
    source: str
    search_method: SearchMethod = "retailer"


@dataclass(slots=True)
class SelectedProductResult:
    title: str
    description: str | None
    image_url: str
    product_url: str | None
    source: str
    search_method: SearchMethod


@dataclass(slots=True)
class ProductResolutionResult:
    status: ResolutionStatus
    brand: str | None
    product_query: str
    raw_user_text: str
    flavor: str | None = None
    volume: str | None = None
    package_type: str | None = None
    selected_result: SelectedProductResult | None = None
    clarification_question: str | None = None
    reason: str | None = None
