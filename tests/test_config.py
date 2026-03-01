from adv_assistant.config import Settings


def test_blank_openai_base_url_is_normalized_to_none(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "   ")
    settings = Settings.from_env()
    assert settings.openai_base_url is None


def test_openai_base_url_preserves_non_empty_value(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.local/v1")
    settings = Settings.from_env()
    assert settings.openai_base_url == "https://example.local/v1"


def test_blank_media_bucket_is_normalized_to_none(monkeypatch) -> None:
    monkeypatch.setenv("MEDIA_GCS_BUCKET", "  ")
    settings = Settings.from_env()
    assert settings.media_gcs_bucket is None
