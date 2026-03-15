import os
from dataclasses import dataclass, field

from adv_assistant.db.session import get_database_url


def _int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return int(raw_value)


def _float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    return float(raw_value)


def _bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _optional_env(name: str) -> str | None:
    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    stripped = raw_value.strip()
    return stripped or None


def _env_with_default(name: str, default: str) -> str:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    stripped = raw_value.strip()
    return stripped or default


@dataclass(slots=True)
class Settings:
    app_name: str = "adv-assistant"
    database_url: str = field(default_factory=get_database_url)
    app_service_role: str = "all"

    meta_verify_token: str = "change-me"
    meta_app_secret: str | None = None
    replay_window_seconds: int = 300
    unauthorized_rejection_window_minutes: int = 60
    unauthorized_rejection_message: str = (
        "You are not authorized to use this assistant. Please contact your administrator."
    )

    whatsapp_access_token: str | None = None
    whatsapp_phone_number_id: str | None = None
    whatsapp_graph_api_version: str = "v21.0"
    whatsapp_media_timeout_seconds: int = 15

    tasks_mode: str = "inline"
    gcp_project_id: str | None = None
    tasks_region: str | None = None
    tasks_queue: str | None = None
    tasks_handler_url: str | None = None
    tasks_service_account_email: str | None = None
    tasks_oidc_audience: str | None = None
    tasks_allowed_service_account_email: str | None = None
    admin_basic_username: str | None = None
    admin_basic_password: str | None = None

    openai_api_key: str | None = None
    openai_base_url: str | None = None
    llm_classification_model: str = "gpt-4o-mini"
    llm_extraction_model: str = "gpt-4o-mini"
    llm_reply_model: str = "gpt-4o-mini"
    llm_max_retries: int = 1
    llm_timeout_seconds: int = 15
    llm_max_input_chars: int = 2000
    llm_trace_enabled: bool = False
    llm_trace_max_chars: int = 4000

    enrichment_enabled: bool = True
    open_food_facts_base_url: str = "https://world.openfoodfacts.org"
    enrichment_http_timeout_seconds: int = 8
    enrichment_max_attempts: int = 2
    enrichment_retry_base_seconds: float = 0.5

    discovery_enabled: bool = False
    shufersal_base_url: str = "https://www.shufersal.co.il"
    discovery_http_timeout_seconds: int = 10
    discovery_max_attempts: int = 2
    discovery_retry_base_seconds: float = 0.5
    serper_api_key: str | None = None
    serper_base_url: str = "https://google.serper.dev"

    gemini_api_key: str | None = None
    gemini_model: str = "gemini-3.1-flash-image-preview"
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    gemini_timeout_seconds: int = 30
    gemini_max_submit_attempts: int = 4
    gemini_retry_base_seconds: float = 1.0

    nana_banana_api_key: str | None = None
    nana_banana_api_url: str | None = None
    nana_banana_base_url: str | None = None
    nana_banana_status_api_url_template: str | None = None
    nana_banana_model: str = "nanobanana-2"
    nana_banana_generation_type: str = "TEXTTOIAMGE"
    nana_banana_num_images: int = 1
    nana_banana_watermark: bool | None = None
    nana_banana_timeout_seconds: int = 20
    nana_banana_poll_initial_seconds: int = 2
    nana_banana_poll_max_seconds: int = 10
    nana_banana_poll_timeout_seconds: int = 900
    ad_render_width: int = 1920
    ad_render_height: int = 1080

    media_store_mode: str = "noop"
    media_gcs_bucket: str | None = None
    media_gcs_public_base_url: str = "https://storage.googleapis.com"
    media_gcs_object_prefix: str = "operator-photos"
    media_s3_bucket: str | None = None
    media_s3_region: str | None = None
    media_s3_public_base_url: str | None = None
    media_s3_object_prefix: str = "operator-photos"
    media_s3_endpoint_url: str | None = None
    media_lifecycle_days: int = 90
    media_verify_lifecycle_on_startup: bool = False

    cms_cityscreen_base_url: str = "https://play.eu.cityscreen.cloud"
    cms_cityscreen_app_token: str | None = None
    cms_cityscreen_campaign_id: int = 157
    cms_cityscreen_playlist_id: int = 139

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            app_name=os.getenv("APP_NAME", "adv-assistant"),
            database_url=get_database_url(),
            app_service_role=_env_with_default("APP_SERVICE_ROLE", "all").lower(),
            meta_verify_token=os.getenv("VERIFY_TOKEN", "change-me"),
            meta_app_secret=os.getenv("META_APP_SECRET"),
            replay_window_seconds=_int_env("REPLAY_WINDOW_SECONDS", 300),
            unauthorized_rejection_window_minutes=_int_env(
                "UNAUTHORIZED_REJECTION_WINDOW_MINUTES", 60
            ),
            unauthorized_rejection_message=os.getenv(
                "UNAUTHORIZED_REJECTION_MESSAGE",
                "You are not authorized to use this assistant. Please contact your administrator.",
            ),
            whatsapp_access_token=os.getenv("WHATSAPP_ACCESS_TOKEN"),
            whatsapp_phone_number_id=os.getenv("PHONE_NUMBER_ID"),
            whatsapp_graph_api_version=os.getenv("GRAPH_API_VERSION", "v21.0"),
            whatsapp_media_timeout_seconds=_int_env("WHATSAPP_MEDIA_TIMEOUT_SECONDS", 15),
            tasks_mode=os.getenv("TASKS_MODE", "inline"),
            gcp_project_id=os.getenv("GCP_PROJECT_ID"),
            tasks_region=os.getenv("TASKS_REGION") or os.getenv("GCP_REGION"),
            tasks_queue=os.getenv("TASKS_QUEUE"),
            tasks_handler_url=os.getenv("TASKS_HANDLER_URL"),
            tasks_service_account_email=os.getenv("TASKS_SERVICE_ACCOUNT_EMAIL"),
            tasks_oidc_audience=os.getenv("TASKS_OIDC_AUDIENCE"),
            tasks_allowed_service_account_email=os.getenv("TASKS_ALLOWED_SERVICE_ACCOUNT_EMAIL"),
            admin_basic_username=_optional_env("ADMIN_BASIC_USERNAME"),
            admin_basic_password=_optional_env("ADMIN_BASIC_PASSWORD"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            openai_base_url=_optional_env("OPENAI_BASE_URL"),
            llm_classification_model=os.getenv("LLM_CLASSIFICATION_MODEL", "gpt-4o-mini"),
            llm_extraction_model=os.getenv("LLM_EXTRACTION_MODEL", "gpt-4o-mini"),
            llm_reply_model=os.getenv("LLM_REPLY_MODEL", "gpt-4o-mini"),
            llm_max_retries=_int_env("LLM_MAX_RETRIES", 1),
            llm_timeout_seconds=_int_env("LLM_TIMEOUT_SECONDS", 15),
            llm_max_input_chars=_int_env("LLM_MAX_INPUT_CHARS", 2000),
            llm_trace_enabled=_bool_env("LLM_TRACE_ENABLED", False),
            llm_trace_max_chars=_int_env("LLM_TRACE_MAX_CHARS", 4000),
            enrichment_enabled=_bool_env("ENRICHMENT_ENABLED", True),
            open_food_facts_base_url=os.getenv(
                "OPEN_FOOD_FACTS_BASE_URL", "https://world.openfoodfacts.org"
            ),
            enrichment_http_timeout_seconds=_int_env("ENRICHMENT_HTTP_TIMEOUT_SECONDS", 8),
            enrichment_max_attempts=_int_env("ENRICHMENT_MAX_ATTEMPTS", 2),
            enrichment_retry_base_seconds=_float_env("ENRICHMENT_RETRY_BASE_SECONDS", 0.5),
            discovery_enabled=_bool_env("DISCOVERY_ENABLED", False),
            shufersal_base_url=_env_with_default(
                "SHUFERSAL_BASE_URL", "https://www.shufersal.co.il"
            ),
            discovery_http_timeout_seconds=_int_env("DISCOVERY_HTTP_TIMEOUT_SECONDS", 10),
            discovery_max_attempts=_int_env("DISCOVERY_MAX_ATTEMPTS", 2),
            discovery_retry_base_seconds=_float_env("DISCOVERY_RETRY_BASE_SECONDS", 0.5),
            serper_api_key=_optional_env("SERPER_API_KEY"),
            serper_base_url=_env_with_default(
                "SERPER_BASE_URL", "https://google.serper.dev"
            ),
            gemini_api_key=_optional_env("GEMINI_API_KEY"),
            gemini_model=_env_with_default("GEMINI_MODEL", "gemini-3.1-flash-image-preview"),
            gemini_base_url=_env_with_default(
                "GEMINI_BASE_URL",
                "https://generativelanguage.googleapis.com/v1beta",
            ),
            gemini_timeout_seconds=_int_env("GEMINI_TIMEOUT_SECONDS", 30),
            gemini_max_submit_attempts=_int_env("GEMINI_MAX_SUBMIT_ATTEMPTS", 4),
            gemini_retry_base_seconds=_float_env("GEMINI_RETRY_BASE_SECONDS", 1.0),
            nana_banana_api_key=_optional_env("NANA_BANANA_API_KEY"),
            nana_banana_api_url=_optional_env("NANA_BANANA_API_URL"),
            nana_banana_base_url=_optional_env("NANA_BANANA_BASE_URL"),
            nana_banana_status_api_url_template=_optional_env(
                "NANA_BANANA_STATUS_API_URL_TEMPLATE"
            ),
            nana_banana_model=_env_with_default("NANA_BANANA_MODEL", "nanobanana-2"),
            nana_banana_generation_type=_env_with_default(
                "NANA_BANANA_GENERATION_TYPE", "TEXTTOIAMGE"
            ),
            nana_banana_num_images=_int_env("NANA_BANANA_NUM_IMAGES", 1),
            nana_banana_watermark=(
                _bool_env("NANA_BANANA_WATERMARK", False)
                if _optional_env("NANA_BANANA_WATERMARK") is not None
                else None
            ),
            nana_banana_timeout_seconds=_int_env("NANA_BANANA_TIMEOUT_SECONDS", 20),
            nana_banana_poll_initial_seconds=_int_env("NANA_BANANA_POLL_INITIAL_SECONDS", 2),
            nana_banana_poll_max_seconds=_int_env("NANA_BANANA_POLL_MAX_SECONDS", 10),
            nana_banana_poll_timeout_seconds=_int_env("NANA_BANANA_POLL_TIMEOUT_SECONDS", 900),
            ad_render_width=_int_env("AD_RENDER_WIDTH", 1920),
            ad_render_height=_int_env("AD_RENDER_HEIGHT", 1080),
            media_store_mode=os.getenv("MEDIA_STORE_MODE", "noop"),
            media_gcs_bucket=_optional_env("MEDIA_GCS_BUCKET"),
            media_gcs_public_base_url=os.getenv(
                "MEDIA_GCS_PUBLIC_BASE_URL", "https://storage.googleapis.com"
            ),
            media_gcs_object_prefix=os.getenv("MEDIA_GCS_OBJECT_PREFIX", "operator-photos"),
            media_s3_bucket=_optional_env("MEDIA_S3_BUCKET"),
            media_s3_region=_optional_env("MEDIA_S3_REGION"),
            media_s3_public_base_url=_optional_env("MEDIA_S3_PUBLIC_BASE_URL"),
            media_s3_object_prefix=os.getenv("MEDIA_S3_OBJECT_PREFIX", "operator-photos"),
            media_s3_endpoint_url=_optional_env("MEDIA_S3_ENDPOINT_URL"),
            media_lifecycle_days=_int_env("MEDIA_LIFECYCLE_DAYS", 90),
            media_verify_lifecycle_on_startup=_bool_env("MEDIA_VERIFY_LIFECYCLE_ON_STARTUP", False),
            cms_cityscreen_base_url=os.getenv(
                "CMS_CITYSCREEN_BASE_URL",
                "https://play.eu.cityscreen.cloud",
            ),
            cms_cityscreen_app_token=_optional_env("CMS_CITYSCREEN_APP_TOKEN"),
            cms_cityscreen_campaign_id=_int_env("CMS_CITYSCREEN_CAMPAIGN_ID", 157),
            cms_cityscreen_playlist_id=_int_env("CMS_CITYSCREEN_PLAYLIST_ID", 139),
        )
