from adv_assistant.config import Settings


def test_blank_openai_base_url_is_normalized_to_none(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "   ")
    settings = Settings.from_env()
    assert settings.openai_base_url is None


def test_openai_base_url_preserves_non_empty_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.local/v1")
    settings = Settings.from_env()
    assert settings.openai_base_url == "https://example.local/v1"


def test_nana_banana_model_and_type_fallback_to_defaults_when_blank(monkeypatch) -> None:
    monkeypatch.setenv("NANA_BANANA_MODEL", "   ")
    monkeypatch.setenv("NANA_BANANA_GENERATION_TYPE", "")
    settings = Settings.from_env()
    assert settings.nana_banana_model == "nanobanana-2"
    assert settings.nana_banana_generation_type == "TEXTTOIAMGE"


def test_blank_media_bucket_is_normalized_to_none(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_GCS_BUCKET", "  ")
    settings = Settings.from_env()
    assert settings.media_gcs_bucket is None


def test_blank_media_s3_bucket_is_normalized_to_none(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_S3_BUCKET", "   ")
    settings = Settings.from_env()
    assert settings.media_s3_bucket is None


def test_blank_nana_banana_watermark_is_normalized_to_none(monkeypatch) -> None:
    monkeypatch.setenv("NANA_BANANA_WATERMARK", "   ")
    settings = Settings.from_env()
    assert settings.nana_banana_watermark is None


def test_gemini_model_falls_back_to_default_when_blank(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_MODEL", "   ")
    settings = Settings.from_env()
    assert settings.gemini_model == "gemini-3.1-flash-image-preview"


def test_app_service_role_defaults_to_all(monkeypatch) -> None:
    monkeypatch.delenv("APP_SERVICE_ROLE", raising=False)
    settings = Settings.from_env()
    assert settings.app_service_role == "all"


def test_app_service_role_is_lowercased(monkeypatch) -> None:
    monkeypatch.setenv("APP_SERVICE_ROLE", "WoRkEr")
    settings = Settings.from_env()
    assert settings.app_service_role == "worker"


def test_llm_trace_settings_are_loaded(monkeypatch) -> None:
    monkeypatch.setenv("LLM_TRACE_ENABLED", "true")
    monkeypatch.setenv("LLM_TRACE_MAX_CHARS", "1234")
    settings = Settings.from_env()
    assert settings.llm_trace_enabled is True
    assert settings.llm_trace_max_chars == 1234


def test_enrichment_retry_settings_are_loaded(monkeypatch) -> None:
    monkeypatch.setenv("ENRICHMENT_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("ENRICHMENT_RETRY_BASE_SECONDS", "0.25")
    settings = Settings.from_env()
    assert settings.enrichment_max_attempts == 4
    assert settings.enrichment_retry_base_seconds == 0.25


def test_enrichment_enabled_can_be_disabled(monkeypatch) -> None:
    monkeypatch.setenv("ENRICHMENT_ENABLED", "false")
    settings = Settings.from_env()
    assert settings.enrichment_enabled is False


def test_cms_cityscreen_app_token_defaults_to_none_when_missing(monkeypatch) -> None:
    monkeypatch.delenv("CMS_CITYSCREEN_APP_TOKEN", raising=False)
    settings = Settings.from_env()
    assert settings.cms_cityscreen_app_token is None
