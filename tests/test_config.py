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


def test_blank_nana_banana_watermark_is_normalized_to_none(monkeypatch) -> None:
    monkeypatch.setenv("NANA_BANANA_WATERMARK", "   ")
    settings = Settings.from_env()
    assert settings.nana_banana_watermark is None


def test_missing_cityscreen_token_remains_none(monkeypatch) -> None:
    monkeypatch.delenv("CMS_CITYSCREEN_APP_TOKEN", raising=False)
    settings = Settings.from_env()
    assert settings.cms_cityscreen_app_token is None


def test_cityscreen_token_comes_only_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CMS_CITYSCREEN_APP_TOKEN", "token-from-env")
    settings = Settings.from_env()
    assert settings.cms_cityscreen_app_token == "token-from-env"
