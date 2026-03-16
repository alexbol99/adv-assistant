from __future__ import annotations

import logging
from dataclasses import dataclass

from adv_assistant.product_resolution_models import ProductCandidate
from adv_assistant.retailer_adapters import (
    CarrefourAdapter,
    RamiLevyAdapter,
    RetailerAdapter,
    ShufersalAdapter,
    VictoryAdapter,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RetailerSearchService:
    adapters: list[RetailerAdapter]

    @classmethod
    def with_default_adapters(
        cls,
        *,
        timeout_seconds: float = 6.0,
        max_results_per_adapter: int = 6,
    ) -> RetailerSearchService:
        return cls(
            adapters=[
                VictoryAdapter(
                    timeout_seconds=timeout_seconds,
                    max_results=max_results_per_adapter,
                ),
                CarrefourAdapter(
                    timeout_seconds=timeout_seconds,
                    max_results=max_results_per_adapter,
                ),
                ShufersalAdapter(
                    timeout_seconds=timeout_seconds,
                    max_results=max_results_per_adapter,
                ),
                RamiLevyAdapter(
                    timeout_seconds=timeout_seconds,
                    max_results=max_results_per_adapter,
                ),
            ]
        )

    async def search(self, *, product_query: str) -> list[ProductCandidate]:
        results: list[ProductCandidate] = []
        for adapter in self.adapters:
            logger.info(
                "Retailer search started (source=%s, query=%s)",
                adapter.source,
                product_query,
            )
            candidates = await adapter.search(query=product_query)
            logger.info(
                "Retailer search completed (source=%s, query=%s, candidates=%s)",
                adapter.source,
                product_query,
                len(candidates),
            )
            results.extend(candidates)
        return results

    async def close(self) -> None:
        for adapter in self.adapters:
            await adapter.close()
