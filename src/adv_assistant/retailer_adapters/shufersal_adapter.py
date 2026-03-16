from __future__ import annotations

import httpx

from adv_assistant.retailer_adapters.base_adapter import GenericRetailerAdapter


class ShufersalAdapter(GenericRetailerAdapter):
    def __init__(
        self,
        *,
        timeout_seconds: float = 6.0,
        max_results: int = 6,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        GenericRetailerAdapter.__init__(
            self,
            source="shufersal",
            search_url_templates=(
                "https://www.shufersal.co.il/online/he/search?text={query}",
                "https://www.shufersal.co.il/online/he/A?CatalogCategoryID=0&query={query}",
            ),
            timeout_seconds=timeout_seconds,
            max_results=max_results,
            client=client,
        )
