from __future__ import annotations

import httpx

from adv_assistant.retailer_adapters.base_adapter import GenericRetailerAdapter


class RamiLevyAdapter(GenericRetailerAdapter):
    def __init__(
        self,
        *,
        timeout_seconds: float = 6.0,
        max_results: int = 6,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        GenericRetailerAdapter.__init__(
            self,
            source="rami_levy",
            search_url_templates=(
                "https://www.rami-levy.co.il/he/online/search?query={query}",
                "https://www.rami-levy.co.il/he/online?search={query}",
            ),
            timeout_seconds=timeout_seconds,
            max_results=max_results,
            client=client,
        )
