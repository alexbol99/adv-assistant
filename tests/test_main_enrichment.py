from adv_assistant.config import Settings
from adv_assistant.enrichment import ProviderChainEnrichmentService
from adv_assistant.main import _build_enrichment_service, _build_product_discovery_service
from adv_assistant.product_discovery import NoopProductDiscoveryService


def test_build_enrichment_service_disabled_uses_no_providers() -> None:
    settings = Settings(enrichment_enabled=False)

    service = _build_enrichment_service(settings)

    assert isinstance(service, ProviderChainEnrichmentService)
    assert service._providers == []


def test_build_enrichment_service_enabled_uses_expected_provider_chain() -> None:
    settings = Settings(enrichment_enabled=True)

    service = _build_enrichment_service(settings)
    providers = service._providers

    assert isinstance(service, ProviderChainEnrichmentService)
    assert [provider.source for provider in providers] == [
        "open_food_facts",
        "ean_fallback",
        "web_search",
    ]


def test_build_product_discovery_service_uses_noop_when_disabled() -> None:
    settings = Settings(product_discovery_enabled=False)

    service = _build_product_discovery_service(settings)

    assert isinstance(service, NoopProductDiscoveryService)
