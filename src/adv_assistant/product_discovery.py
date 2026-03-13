from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ProductDiscoveryStatus(StrEnum):
    SINGLE_MATCH = "single_match"
    MULTIPLE_CANDIDATES = "multiple_candidates"
    NO_MATCH = "no_match"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True, frozen=True)
class ProductDiscoveryCandidate:
    name: str
    image_url: str | None = None
    source: str = "unknown"
    confidence: float | None = None
    external_ref: str | None = None


@dataclass(slots=True, frozen=True)
class ProductDiscoveryResult:
    status: ProductDiscoveryStatus
    candidates: tuple[ProductDiscoveryCandidate, ...] = ()
    reason: str | None = None


class ProductDiscoveryService(Protocol):
    enabled: bool

    async def discover(
        self,
        *,
        language: str,
        message_text: str | None,
        product_name: str | None,
        photo_url: str | None,
        ean: str | None,
    ) -> ProductDiscoveryResult: ...

    async def close(self) -> None: ...


class NoopProductDiscoveryService:
    enabled = False

    def __init__(self, *, reason: str = "unconfigured") -> None:
        self._reason = reason

    async def discover(
        self,
        *,
        language: str,
        message_text: str | None,
        product_name: str | None,
        photo_url: str | None,
        ean: str | None,
    ) -> ProductDiscoveryResult:
        return ProductDiscoveryResult(
            status=ProductDiscoveryStatus.UNAVAILABLE,
            reason=self._reason,
        )

    async def close(self) -> None:
        return None
