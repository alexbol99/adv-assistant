import logging
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.ad_generation import (
    GeminiFlashImageAdGenerationService,
    NanoBananaAdGenerationService,
    NoopAdGenerationService,
)
from adv_assistant.ai_extractor import HeuristicProductAIExtractor, OpenAIProductAIExtractor
from adv_assistant.candidate_selector import CandidateSelector
from adv_assistant.cms_cityscreen import CityScreenCMSPublisher, CMSPublisher, NoopCMSPublisher
from adv_assistant.config import Settings
from adv_assistant.db.base import utcnow
from adv_assistant.db.models import Operator
from adv_assistant.db.repositories import (
    AuditEventRepository,
    OperatorRepository,
    ProcessedInboundMessageRepository,
)
from adv_assistant.db.session import create_engine, create_session_factory, session_scope
from adv_assistant.enrichment import (
    NoopProductLookupProvider,
    OpenFoodFactsProvider,
    ProviderChainEnrichmentService,
)
from adv_assistant.ingress import (
    extract_inbound_messages,
    is_within_replay_window,
    normalize_phone_number,
    verify_x_hub_signature,
)
from adv_assistant.llm_gateway import (
    BUTTON_CANCEL_PUBLISH,
    BUTTON_CONFIRM_PUBLISH,
    NoopLLMGateway,
    OpenAILLMGateway,
)
from adv_assistant.media_ingest import (
    DefaultOperatorPhotoIngestor,
    NoopWhatsAppMediaClient,
    OperatorPhotoIngestor,
    WhatsAppMediaClient,
)
from adv_assistant.media_store import GCSMediaStore, MediaStore, NoopMediaStore, S3MediaStore
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.product_discovery import (
    NoopProductDiscoveryService,
    ProductDiscoveryService,
    ProviderChainDiscoveryService,
    SerperImageSearchProvider,
    ShufersalSearchProvider,
)
from adv_assistant.product_resolution_service import (
    DefaultProductResolutionService,
    NoopProductResolutionService,
    ProductResolutionService,
)
from adv_assistant.retailer_search_service import RetailerSearchService
from adv_assistant.serper_image_search_service import SerperImageSearchService
from adv_assistant.tasks_auth import (
    OidcTaskRequestAuthorizer,
    RejectAllTaskRequestAuthorizer,
    TaskRequestAuthorizer,
)
from adv_assistant.tasks_queue import (
    CloudTasksEnqueuer,
    DisabledTaskEnqueuer,
    InboundTaskPayload,
    InlineTaskEnqueuer,
    TaskEnqueuer,
)
from adv_assistant.whatsapp import (
    MetaWhatsAppClient,
    MetaWhatsAppMediaClient,
    NoopWhatsAppClient,
    WhatsAppClient,
)

logger = logging.getLogger(__name__)

_ADMIN_AUTH_CHALLENGE = {"WWW-Authenticate": "Basic"}


class OperatorCMSConnectRequest(BaseModel):
    phone: str = Field(max_length=32)
    cms_campaign_id: int = Field(gt=0)
    cms_playlist_id: int = Field(gt=0)
    active: bool = True


class OperatorCMSMappingResponse(BaseModel):
    phone: str
    display_name: str | None
    language: str
    currency: str
    cms_campaign_id: int | None
    cms_playlist_id: int | None
    business_name: str | None
    logo_url: str | None
    brand_colors: list[str] | None
    store_type: str | None
    creative_guidance: str | None
    active: bool
    created_at: datetime
    updated_at: datetime


class OperatorTraceEventResponse(BaseModel):
    id: str
    action: str
    operator_phone: str | None
    timestamp: datetime
    metadata: dict[str, Any]


def _build_task_authorizer(settings: Settings) -> TaskRequestAuthorizer:
    audience = settings.tasks_oidc_audience or settings.tasks_handler_url
    if audience:
        return OidcTaskRequestAuthorizer(
            audience=audience,
            allowed_service_account_email=settings.tasks_allowed_service_account_email,
        )
    return RejectAllTaskRequestAuthorizer()


def _build_whatsapp_client(settings: Settings) -> WhatsAppClient:
    if settings.whatsapp_access_token and settings.whatsapp_phone_number_id:
        return MetaWhatsAppClient(
            access_token=settings.whatsapp_access_token,
            phone_number_id=settings.whatsapp_phone_number_id,
            graph_api_version=settings.whatsapp_graph_api_version,
        )
    return NoopWhatsAppClient()


def _build_enrichment_service(settings: Settings) -> ProviderChainEnrichmentService:
    if not settings.enrichment_enabled:
        return ProviderChainEnrichmentService(providers=[])
    return ProviderChainEnrichmentService(
        providers=[
            OpenFoodFactsProvider(
                base_url=settings.open_food_facts_base_url,
                timeout_seconds=settings.enrichment_http_timeout_seconds,
                max_attempts=settings.enrichment_max_attempts,
                retry_base_seconds=settings.enrichment_retry_base_seconds,
            ),
            NoopProductLookupProvider("ean_fallback"),
            NoopProductLookupProvider("web_search"),
        ]
    )


def _build_product_discovery_service(settings: Settings) -> ProductDiscoveryService:
    if not settings.discovery_enabled:
        return NoopProductDiscoveryService(reason="disabled")

    providers: list[ShufersalSearchProvider | SerperImageSearchProvider] = [
        ShufersalSearchProvider(
            base_url=settings.shufersal_base_url,
            timeout_seconds=settings.discovery_http_timeout_seconds,
            max_attempts=settings.discovery_max_attempts,
            retry_base_seconds=settings.discovery_retry_base_seconds,
        ),
    ]
    if settings.serper_api_key:
        providers.append(
            SerperImageSearchProvider(
                api_key=settings.serper_api_key,
                base_url=settings.serper_base_url,
                timeout_seconds=settings.discovery_http_timeout_seconds,
                max_attempts=settings.discovery_max_attempts,
                retry_base_seconds=settings.discovery_retry_base_seconds,
            ),
        )
    return ProviderChainDiscoveryService(providers=providers)  # type: ignore[arg-type]


def _build_product_resolution_service(settings: Settings) -> ProductResolutionService:
    if not settings.product_resolution_enabled:
        return NoopProductResolutionService()

    if settings.openai_api_key:
        extractor = OpenAIProductAIExtractor(
            api_key=settings.openai_api_key,
            model=settings.product_resolution_openai_model,
            base_url=settings.openai_base_url,
            timeout_seconds=settings.product_resolution_timeout_seconds,
            max_input_chars=settings.product_resolution_max_input_chars,
            fallback_extractor=HeuristicProductAIExtractor(),
        )
    else:
        extractor = HeuristicProductAIExtractor()

    retailer_search_service = RetailerSearchService.with_default_adapters(
        timeout_seconds=settings.product_resolution_retailer_timeout_seconds,
        max_results_per_adapter=settings.product_resolution_retailer_max_results,
    )
    serper_service = SerperImageSearchService(
        api_key=settings.serper_api_key,
        base_url=settings.serper_base_url,
        timeout_seconds=settings.serper_timeout_seconds,
        max_results=settings.serper_max_results,
    )
    selector = CandidateSelector(minimum_score=settings.product_resolution_min_selector_score)
    return DefaultProductResolutionService(
        ai_extractor=extractor,
        retailer_search_service=retailer_search_service,
        serper_image_search_service=serper_service,
        candidate_selector=selector,
    )


def _build_ad_generation_service(*, settings: Settings, media_store: MediaStore):
    if settings.gemini_api_key:
        return GeminiFlashImageAdGenerationService(
            api_key=settings.gemini_api_key,
            media_store=media_store,
            model=settings.gemini_model,
            base_url=settings.gemini_base_url,
            timeout_seconds=settings.gemini_timeout_seconds,
            max_submit_attempts=settings.gemini_max_submit_attempts,
            retry_base_seconds=settings.gemini_retry_base_seconds,
            trace_enabled=settings.llm_trace_enabled,
            trace_max_chars=settings.llm_trace_max_chars,
        )

    required = {
        "NANA_BANANA_API_KEY": settings.nana_banana_api_key,
    }
    has_api_target = bool(settings.nana_banana_api_url or settings.nana_banana_base_url)
    if all(required.values()) and has_api_target:
        return NanoBananaAdGenerationService(
            api_key=settings.nana_banana_api_key or "",
            api_url=settings.nana_banana_api_url,
            base_url=settings.nana_banana_base_url or "",
            status_api_url_template=settings.nana_banana_status_api_url_template,
            model=settings.nana_banana_model,
            generation_type=settings.nana_banana_generation_type,
            num_images=settings.nana_banana_num_images,
            watermark=settings.nana_banana_watermark,
            timeout_seconds=settings.nana_banana_timeout_seconds,
            poll_initial_seconds=settings.nana_banana_poll_initial_seconds,
            poll_max_seconds=settings.nana_banana_poll_max_seconds,
            poll_timeout_seconds=settings.nana_banana_poll_timeout_seconds,
        )
    return NoopAdGenerationService()


def _build_media_store(settings: Settings) -> MediaStore:
    mode = settings.media_store_mode.strip().lower()
    if mode == "noop":
        return NoopMediaStore()
    if mode == "gcs":
        if not settings.media_gcs_bucket:
            raise RuntimeError("MEDIA_GCS_BUCKET is required when MEDIA_STORE_MODE=gcs")
        return GCSMediaStore(
            bucket_name=settings.media_gcs_bucket,
            project_id=settings.gcp_project_id,
            public_base_url=settings.media_gcs_public_base_url,
            object_prefix=settings.media_gcs_object_prefix,
        )
    if mode == "s3":
        if not settings.media_s3_bucket:
            raise RuntimeError("MEDIA_S3_BUCKET is required when MEDIA_STORE_MODE=s3")
        return S3MediaStore(
            bucket_name=settings.media_s3_bucket,
            region_name=settings.media_s3_region,
            public_base_url=settings.media_s3_public_base_url,
            object_prefix=settings.media_s3_object_prefix,
            endpoint_url=settings.media_s3_endpoint_url,
        )
    raise RuntimeError(f"Unsupported MEDIA_STORE_MODE='{settings.media_store_mode}'")


def _build_whatsapp_media_client(settings: Settings) -> WhatsAppMediaClient:
    if settings.whatsapp_access_token:
        return MetaWhatsAppMediaClient(
            access_token=settings.whatsapp_access_token,
            graph_api_version=settings.whatsapp_graph_api_version,
            timeout_seconds=settings.whatsapp_media_timeout_seconds,
        )
    return NoopWhatsAppMediaClient()


def _build_cms_publisher(settings: Settings) -> CMSPublisher:
    if settings.cms_cityscreen_app_token:
        return CityScreenCMSPublisher(
            base_url=settings.cms_cityscreen_base_url,
            app_token=settings.cms_cityscreen_app_token,
            campaign_id=settings.cms_cityscreen_campaign_id,
            playlist_id=settings.cms_cityscreen_playlist_id,
            expected_width=settings.ad_render_width,
            expected_height=settings.ad_render_height,
        )
    return NoopCMSPublisher()


def _build_operator_photo_ingestor(
    *,
    media_store: MediaStore,
    whatsapp_media_client: WhatsAppMediaClient,
) -> OperatorPhotoIngestor:
    return DefaultOperatorPhotoIngestor(
        media_client=whatsapp_media_client,
        media_store=media_store,
    )


def _build_task_enqueuer(
    *,
    settings: Settings,
    process_callback: Callable[[InboundTaskPayload], Awaitable[object]],
) -> TaskEnqueuer:
    if settings.tasks_mode == "cloud":
        required = {
            "GCP_PROJECT_ID": settings.gcp_project_id,
            "TASKS_REGION": settings.tasks_region,
            "TASKS_QUEUE": settings.tasks_queue,
            "TASKS_HANDLER_URL": settings.tasks_handler_url,
            "TASKS_SERVICE_ACCOUNT_EMAIL": settings.tasks_service_account_email,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise RuntimeError(
                f"Cloud Tasks mode requires missing settings: {', '.join(sorted(missing))}"
            )
        return CloudTasksEnqueuer(
            project_id=settings.gcp_project_id or "",
            location=settings.tasks_region or "",
            queue_name=settings.tasks_queue or "",
            handler_url=settings.tasks_handler_url or "",
            service_account_email=settings.tasks_service_account_email or "",
            oidc_audience=settings.tasks_oidc_audience or settings.tasks_handler_url,
        )
    return InlineTaskEnqueuer(process_callback)


async def _should_send_unauthorized_rejection(
    *,
    audit_repo: AuditEventRepository,
    phone: str,
    now_value: datetime,
    window_minutes: int,
) -> bool:
    return not await audit_repo.has_action_since(
        action="unauthorized_rejection_sent",
        operator_phone=phone,
        since=now_value - timedelta(minutes=window_minutes),
    )


async def _validate_schema_compatibility(
    *,
    engine,
    settings: Settings,
) -> None:
    required_columns_by_table: dict[str, set[str]] = {
        "operator": {
            "business_name",
            "logo_url",
            "brand_colors",
            "cms_campaign_id",
            "cms_playlist_id",
            "store_type",
            "creative_guidance",
        },
        "conversation_session": {
            "pending_upload_type",
            "pending_followup_question",
            "pending_question_type",
            "pending_question_context",
        },
        "ad_draft": {"product_brand"},
    }
    if settings.enrichment_enabled:
        required_columns_by_table["ad_draft"].update(
            {
                "enriched_brand",
                "enriched_category",
                "enriched_description",
                "enriched_image_url",
                "enrichment_source",
                "enrichment_unavailable_notified_at",
            }
        )

    async with engine.begin() as connection:
        table_names, columns_by_table = await connection.run_sync(_inspect_schema)

    for table_name in required_columns_by_table:
        if table_name in table_names:
            continue
        raise RuntimeError(
            f"Database schema is not initialized (missing '{table_name}'). "
            "Run `uv run alembic upgrade head`."
        )

    for table_name, required_columns in required_columns_by_table.items():
        table_columns = columns_by_table.get(table_name, set())
        missing_columns = sorted(required_columns - table_columns)
        if not missing_columns:
            continue
        raise RuntimeError(
            "Database schema is behind application code. "
            f"Missing columns in '{table_name}': {', '.join(missing_columns)}. "
            "Run `uv run alembic upgrade head`."
        )


def _inspect_schema(sync_connection) -> tuple[set[str], dict[str, set[str]]]:
    schema_inspector = inspect(sync_connection)
    table_names = set(schema_inspector.get_table_names())
    columns_by_table: dict[str, set[str]] = {}
    for table_name in ("operator", "conversation_session", "ad_draft"):
        if table_name not in table_names:
            continue
        columns_by_table[table_name] = {
            column["name"] for column in schema_inspector.get_columns(table_name)
        }
    return table_names, columns_by_table


async def _validate_media_lifecycle(
    *,
    settings: Settings,
    media_store: MediaStore,
) -> None:
    if not settings.media_verify_lifecycle_on_startup:
        return
    if settings.media_store_mode.strip().lower() != "gcs":
        return
    has_rule = await media_store.has_delete_lifecycle_rule(days=settings.media_lifecycle_days)
    if not has_rule:
        raise RuntimeError(
            "GCS lifecycle policy mismatch: expected a Delete rule with age="
            f"{settings.media_lifecycle_days} days on bucket '{settings.media_gcs_bucket}'."
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or Settings.from_env()
    service_role = current_settings.app_service_role.strip().lower()
    if service_role not in {"all", "webhook", "worker"}:
        raise RuntimeError("APP_SERVICE_ROLE must be one of: all, webhook, worker")
    webhook_enabled = service_role in {"all", "webhook"}
    worker_enabled = service_role in {"all", "worker"}

    engine = create_engine(current_settings.database_url)
    session_factory = create_session_factory(engine)
    if current_settings.openai_api_key:
        llm_gateway = OpenAILLMGateway(
            api_key=current_settings.openai_api_key,
            base_url=current_settings.openai_base_url,
            classification_model=current_settings.llm_classification_model,
            extraction_model=current_settings.llm_extraction_model,
            reply_model=current_settings.llm_reply_model,
            max_retries=current_settings.llm_max_retries,
            timeout_seconds=current_settings.llm_timeout_seconds,
            max_input_chars=current_settings.llm_max_input_chars,
            trace_enabled=current_settings.llm_trace_enabled,
            trace_max_chars=current_settings.llm_trace_max_chars,
        )
    else:
        llm_gateway = NoopLLMGateway()

    whatsapp_client = _build_whatsapp_client(current_settings)
    whatsapp_media_client = _build_whatsapp_media_client(current_settings)
    enrichment_service = _build_enrichment_service(current_settings)
    product_discovery_service = _build_product_discovery_service(current_settings)
    product_resolution_service = _build_product_resolution_service(current_settings)
    media_store = _build_media_store(current_settings)
    cms_publisher = _build_cms_publisher(current_settings)
    ad_generation_service = _build_ad_generation_service(
        settings=current_settings,
        media_store=media_store,
    )
    operator_photo_ingestor = _build_operator_photo_ingestor(
        media_store=media_store,
        whatsapp_media_client=whatsapp_media_client,
    )
    task_processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=llm_gateway,
        enrichment_service=enrichment_service,
        product_discovery_service=product_discovery_service,
        ad_generation_service=ad_generation_service,
        render_width=current_settings.ad_render_width,
        render_height=current_settings.ad_render_height,
        operator_photo_ingestor=operator_photo_ingestor,
        cms_publisher=cms_publisher,
        whatsapp_client=whatsapp_client,
        product_resolution_service=product_resolution_service,
    )

    async def process_and_maybe_send_reply(payload: InboundTaskPayload):
        result = await task_processor.process(payload)
        if (
            (
                result.generated_image_url
                or result.variant_image_urls
                or result.reply_text
                or result.publish_buttons_prompt
                or result.action_buttons
            )
            and not result.duplicate
            and not result.unauthorized_operator
        ):
            try:
                if result.variant_image_urls and len(result.variant_image_urls) == 2:
                    # Send both variant images. First with caption, second without.
                    await app.state.whatsapp_client.send_image(
                        to_phone=payload.operator_phone,
                        image_url=result.variant_image_urls[0],
                        caption=f"A\n{result.reply_text}" if result.reply_text else "A",
                    )
                    await app.state.whatsapp_client.send_image(
                        to_phone=payload.operator_phone,
                        image_url=result.variant_image_urls[1],
                        caption="B",
                    )
                elif result.generated_image_url:
                    await app.state.whatsapp_client.send_image(
                        to_phone=payload.operator_phone,
                        image_url=result.generated_image_url,
                        caption=result.reply_text,
                    )
                elif result.reply_text:
                    await app.state.whatsapp_client.send_text(
                        to_phone=payload.operator_phone,
                        message=result.reply_text,
                    )
                if result.action_buttons:
                    await app.state.whatsapp_client.send_buttons(
                        to_phone=payload.operator_phone,
                        body_text=result.action_buttons_prompt or "",
                        buttons=result.action_buttons,
                    )
                elif result.publish_buttons_prompt:
                    await app.state.whatsapp_client.send_buttons(
                        to_phone=payload.operator_phone,
                        body_text=result.publish_buttons_prompt,
                        buttons=[
                            (BUTTON_CONFIRM_PUBLISH, "Apply"),
                            (BUTTON_CANCEL_PUBLISH, "Cancel"),
                        ],
                    )
            except Exception as exc:
                logger.exception(
                    "Outbound reply delivery failed (wamid=%s, operator_phone=%s)",
                    payload.wamid,
                    payload.operator_phone,
                )
                async with session_scope(session_factory) as cleanup_session:
                    processed_repo = ProcessedInboundMessageRepository(cleanup_session)
                    audit_repo = AuditEventRepository(cleanup_session)
                    deleted = await processed_repo.delete_by_wamid(payload.wamid)
                    await audit_repo.log(
                        actor="system",
                        action="outbound_reply_delivery_failed",
                        operator_phone=payload.operator_phone,
                        metadata={
                            "wamid": payload.wamid,
                            "dedup_deleted": deleted,
                            "error": str(exc),
                        },
                    )
                raise
            else:
                try:
                    async with session_scope(session_factory) as audit_session:
                        await AuditEventRepository(audit_session).log(
                            actor="system",
                            action="outbound_reply_sent",
                            operator_phone=payload.operator_phone,
                            metadata={
                                "wamid": payload.wamid,
                                "sent_text": bool(result.reply_text),
                                "sent_image": bool(result.generated_image_url),
                                "sent_buttons": bool(
                                    result.publish_buttons_prompt or result.action_buttons
                                ),
                            },
                        )
                except Exception:
                    pass
        return result

    if webhook_enabled:
        task_enqueuer = _build_task_enqueuer(
            settings=current_settings,
            process_callback=process_and_maybe_send_reply,
        )
    else:
        task_enqueuer = DisabledTaskEnqueuer()
    task_authorizer = _build_task_authorizer(current_settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            await _validate_schema_compatibility(engine=engine, settings=current_settings)
            await _validate_media_lifecycle(settings=current_settings, media_store=media_store)
            yield
        finally:
            await operator_photo_ingestor.close()
            await whatsapp_client.close()
            await whatsapp_media_client.close()
            await enrichment_service.close()
            await product_discovery_service.close()
            await product_resolution_service.close()
            await ad_generation_service.close()
            await cms_publisher.close()
            await media_store.close()
            await engine.dispose()

    app = FastAPI(title=current_settings.app_name, lifespan=lifespan)

    app.state.settings = current_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.task_processor = task_processor
    app.state.task_enqueuer = task_enqueuer
    app.state.task_authorizer = task_authorizer
    app.state.whatsapp_client = whatsapp_client
    app.state.product_discovery_service = product_discovery_service
    app.state.ad_generation_service = ad_generation_service
    app.state.product_resolution_service = product_resolution_service
    app.state.whatsapp_media_client = whatsapp_media_client
    app.state.media_store = media_store
    app.state.cms_publisher = cms_publisher
    app.state.process_and_maybe_send_reply = process_and_maybe_send_reply
    app.state.webhook_enabled = webhook_enabled
    app.state.worker_enabled = worker_enabled

    def get_settings() -> Settings:
        return app.state.settings

    def get_session_factory() -> async_sessionmaker[AsyncSession]:
        return app.state.session_factory

    async def get_db_session(
        session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
    ) -> AsyncIterator[AsyncSession]:
        async with session_scope(session_factory) as session:
            yield session

    def get_task_enqueuer() -> TaskEnqueuer:
        return app.state.task_enqueuer

    def get_task_authorizer() -> TaskRequestAuthorizer:
        return app.state.task_authorizer

    def get_process_inbound_callback() -> Callable[[InboundTaskPayload], Awaitable[object]]:
        return app.state.process_and_maybe_send_reply

    def get_whatsapp_client() -> WhatsAppClient:
        return app.state.whatsapp_client

    SettingsDep = Annotated[Settings, Depends(get_settings)]
    DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
    TaskEnqueuerDep = Annotated[TaskEnqueuer, Depends(get_task_enqueuer)]
    TaskAuthorizerDep = Annotated[TaskRequestAuthorizer, Depends(get_task_authorizer)]
    ProcessInboundDep = Annotated[
        Callable[[InboundTaskPayload], Awaitable[object]],
        Depends(get_process_inbound_callback),
    ]
    WhatsAppClientDep = Annotated[WhatsAppClient, Depends(get_whatsapp_client)]

    HubMode = Annotated[str, Query(alias="hub.mode")]
    HubToken = Annotated[str, Query(alias="hub.verify_token")]
    HubChallenge = Annotated[str, Query(alias="hub.challenge")]
    basic_auth = HTTPBasic(auto_error=False)

    def _normalize_optional_text(value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        return stripped or None

    def _normalize_optional_phone(value: str | None) -> str | None:
        normalized = _normalize_optional_text(value)
        if normalized is None:
            return None
        return normalize_phone_number(normalized)

    async def _require_admin(
        credentials: Annotated[HTTPBasicCredentials | None, Depends(basic_auth)],
    ) -> None:
        expected_username = current_settings.admin_basic_username
        expected_password = current_settings.admin_basic_password
        if not expected_username or not expected_password:
            raise HTTPException(status_code=503, detail="Admin authentication is not configured")
        if credentials is None:
            raise HTTPException(
                status_code=401,
                detail="Missing admin credentials",
                headers=_ADMIN_AUTH_CHALLENGE,
            )
        username_ok = secrets.compare_digest(credentials.username, expected_username)
        password_ok = secrets.compare_digest(credentials.password, expected_password)
        if not (username_ok and password_ok):
            raise HTTPException(
                status_code=401,
                detail="Invalid admin credentials",
                headers=_ADMIN_AUTH_CHALLENGE,
            )

    AdminAuthDep = Annotated[None, Depends(_require_admin)]

    def _to_operator_mapping_response(operator: Operator) -> OperatorCMSMappingResponse:
        return OperatorCMSMappingResponse(
            phone=operator.phone,
            display_name=operator.display_name,
            language=operator.language,
            currency=operator.currency,
            cms_campaign_id=operator.cms_campaign_id,
            cms_playlist_id=operator.cms_playlist_id,
            business_name=operator.business_name,
            logo_url=operator.logo_url,
            brand_colors=operator.brand_colors,
            store_type=operator.store_type,
            creative_guidance=operator.creative_guidance,
            active=operator.active,
            created_at=operator.created_at,
            updated_at=operator.updated_at,
        )

    def _to_operator_trace_event_response(event) -> OperatorTraceEventResponse:
        return OperatorTraceEventResponse(
            id=str(event.id),
            action=event.action,
            operator_phone=event.operator_phone,
            timestamp=event.timestamp,
            metadata=event.metadata_json,
        )

    async def _upsert_operator_cms_mapping(
        *,
        session: AsyncSession,
        phone: str,
        cms_campaign_id: int,
        cms_playlist_id: int,
        active: bool,
    ):
        normalized_phone = _normalize_optional_phone(phone)
        if normalized_phone is None:
            raise HTTPException(
                status_code=422,
                detail="Phone is required",
            )

        operator_repo = OperatorRepository(session)
        audit_repo = AuditEventRepository(session)

        operator = await operator_repo.get_by_phone(normalized_phone)

        if operator is None:
            operator = await operator_repo.create(
                phone=normalized_phone,
                active=active,
                cms_campaign_id=cms_campaign_id,
                cms_playlist_id=cms_playlist_id,
            )
            await audit_repo.log(
                actor="admin",
                action="admin_operator_created_with_cms_mapping",
                operator_phone=operator.phone,
                metadata={
                    "cms_campaign_id": cms_campaign_id,
                    "cms_playlist_id": cms_playlist_id,
                },
            )
            return operator

        await operator_repo.update_cms_mapping(
            operator.phone,
            cms_campaign_id=cms_campaign_id,
            cms_playlist_id=cms_playlist_id,
        )
        if operator.active != active:
            await operator_repo.set_active(operator.phone, active)

        updated_operator = await operator_repo.get_by_phone(operator.phone)
        if updated_operator is None:
            raise HTTPException(status_code=500, detail="Operator update failed")

        await audit_repo.log(
            actor="admin",
            action="admin_operator_cms_mapping_updated",
            operator_phone=updated_operator.phone,
            metadata={
                "cms_campaign_id": updated_operator.cms_campaign_id,
                "cms_playlist_id": updated_operator.cms_playlist_id,
                "active": updated_operator.active,
            },
        )
        return updated_operator

    @app.get("/admin", response_class=HTMLResponse)
    async def admin_home(_: AdminAuthDep) -> HTMLResponse:
        return HTMLResponse(
            content="""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Adv Assistant Admin</title>
    <style>
      body { font-family: Arial, sans-serif; margin: 2rem; max-width: 760px; }
      fieldset { margin-bottom: 1rem; padding: 1rem; }
      label { display: block; margin-top: 0.6rem; }
      input[type=text], input[type=number] { width: 100%; padding: 0.4rem; }
      button { margin-top: 1rem; padding: 0.5rem 1rem; }
      .note { color: #444; font-size: 0.95rem; }
    </style>
  </head>
  <body>
    <h1>Operator CMS Mapping</h1>
    <p class="note">Provide phone plus campaign/playlist IDs.</p>
    <form id="mapping-form">
      <fieldset>
        <label>Phone</label>
        <input id="phone" type="text" name="phone" placeholder="+9725..." required />

        <label>CMS Campaign ID</label>
        <input id="cms_campaign_id" type="number" name="cms_campaign_id" min="1" required />

        <label>CMS Playlist ID</label>
        <input id="cms_playlist_id" type="number" name="cms_playlist_id" min="1" required />

        <label><input id="active" type="checkbox" name="active" checked /> Active</label>

        <button type="submit">Save Mapping</button>
      </fieldset>
    </form>
    <fieldset>
      <legend>Operator Profile Lookup</legend>
      <label>Lookup by Phone</label>
      <input id="lookup_phone" type="text" placeholder="+9725..." />
      <button id="lookup-by-phone" type="button">Get By Phone</button>
    </fieldset>
    <fieldset>
      <legend>LLM / Gemini Trace Lookup</legend>
      <p class="note">Trace events are written only when LLM_TRACE_ENABLED=true.</p>
      <label>Phone</label>
      <input id="trace_phone" type="text" placeholder="+9725..." />
      <label>Limit (1-100)</label>
      <input id="trace_limit" type="number" min="1" max="100" value="20" />
      <button id="lookup-traces" type="button">Get Traces</button>
    </fieldset>
    <pre id="result"></pre>
    <pre id="trace-result"></pre>
    <script>
      const form = document.getElementById('mapping-form');
      const result = document.getElementById('result');
      const traceResult = document.getElementById('trace-result');
      const lookupByPhoneButton = document.getElementById('lookup-by-phone');
      const lookupTracesButton = document.getElementById('lookup-traces');

      const writeResult = async (response) => {
        const body = await response.text();
        result.textContent = `${response.status} ${response.statusText}\\n${body}`;
      };

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const payload = {
          phone: document.getElementById('phone').value || null,
          cms_campaign_id: Number(document.getElementById('cms_campaign_id').value),
          cms_playlist_id: Number(document.getElementById('cms_playlist_id').value),
          active: document.getElementById('active').checked,
        };
        const response = await fetch('/admin/operators/connect', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        await writeResult(response);
      });

      lookupByPhoneButton.addEventListener('click', async () => {
        const phone = document.getElementById('lookup_phone').value.trim();
        if (!phone) {
          result.textContent = 'Please provide a phone value.';
          return;
        }
        const response = await fetch(`/admin/operators/by-phone/${encodeURIComponent(phone)}`);
        await writeResult(response);
      });

      lookupTracesButton.addEventListener('click', async () => {
        const phone = document.getElementById('trace_phone').value.trim();
        if (!phone) {
          traceResult.textContent = 'Please provide a phone value for trace lookup.';
          return;
        }
        const limitRaw = Number(document.getElementById('trace_limit').value);
        const limit = Number.isFinite(limitRaw)
          ? Math.min(100, Math.max(1, Math.trunc(limitRaw)))
          : 20;
        const response = await fetch(
          `/admin/operators/${encodeURIComponent(phone)}/traces?limit=${limit}`
        );
        const body = await response.text();
        traceResult.textContent = `${response.status} ${response.statusText}\\n${body}`;
      });
    </script>
  </body>
</html>
""".strip()
        )

    @app.post("/admin/operators/connect", response_model=OperatorCMSMappingResponse)
    async def admin_connect_operator_json(
        body: OperatorCMSConnectRequest,
        session: DbSessionDep,
        _: AdminAuthDep,
    ) -> OperatorCMSMappingResponse:
        try:
            operator = await _upsert_operator_cms_mapping(
                session=session,
                phone=body.phone,
                cms_campaign_id=body.cms_campaign_id,
                cms_playlist_id=body.cms_playlist_id,
                active=body.active,
            )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=409,
                detail="Operator mapping update conflicted",
            ) from exc
        return _to_operator_mapping_response(operator)

    @app.get("/admin/operators/by-phone/{phone}", response_model=OperatorCMSMappingResponse)
    async def admin_get_operator_by_phone(
        phone: str,
        session: DbSessionDep,
        _: AdminAuthDep,
    ) -> OperatorCMSMappingResponse:
        normalized_phone = normalize_phone_number(phone)
        operator = await OperatorRepository(session).get_by_phone(normalized_phone)
        if operator is None:
            raise HTTPException(status_code=404, detail="Operator not found")
        return _to_operator_mapping_response(operator)

    @app.get(
        "/admin/operators/{phone}/traces",
        response_model=list[OperatorTraceEventResponse],
    )
    async def admin_get_operator_traces(
        phone: str,
        session: DbSessionDep,
        _: AdminAuthDep,
        limit: int = Query(default=20, ge=1, le=100),
    ) -> list[OperatorTraceEventResponse]:
        normalized_phone = normalize_phone_number(phone)
        events = await AuditEventRepository(session).list_recent_for_operator_actions(
            operator_phone=normalized_phone,
            actions=["openai_request_debug", "gemini_request_debug"],
            limit=limit,
        )
        return [_to_operator_trace_event_response(event) for event in events]

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    if app.state.webhook_enabled:

        @app.get("/webhook")
        async def verify_webhook(
            settings_value: SettingsDep,
            mode: HubMode = "",
            verify_token: HubToken = "",
            challenge: HubChallenge = "",
        ) -> PlainTextResponse:
            if mode == "subscribe" and verify_token == settings_value.meta_verify_token:
                return PlainTextResponse(content=challenge, status_code=200)
            raise HTTPException(status_code=403, detail="Verification failed")

        @app.post("/webhook")
        async def receive_webhook(
            request: Request,
            session: DbSessionDep,
            settings_value: SettingsDep,
            task_enqueuer_value: TaskEnqueuerDep,
            whatsapp_client_value: WhatsAppClientDep,
        ) -> dict[str, int | str]:
            body = await request.body()
            signature_header = request.headers.get("X-Hub-Signature-256")
            if not verify_x_hub_signature(
                app_secret=settings_value.meta_app_secret,
                payload_body=body,
                signature_header=signature_header,
            ):
                raise HTTPException(status_code=401, detail="Invalid signature")

            payload = await request.json()
            events = extract_inbound_messages(payload)
            now_value = utcnow()
            operator_repo = OperatorRepository(session)
            audit_repo = AuditEventRepository(session)

            enqueued_count = 0
            for event in events:
                if not is_within_replay_window(
                    message_timestamp=event.timestamp,
                    now=now_value,
                    replay_window_seconds=settings_value.replay_window_seconds,
                ):
                    await audit_repo.log(
                        actor="system",
                        action="webhook_replay_rejected",
                        operator_phone=event.operator_phone,
                        metadata={"wamid": event.wamid},
                    )
                    continue

                operator = await operator_repo.get_by_phone(event.operator_phone)
                if not operator or not operator.active:
                    if await _should_send_unauthorized_rejection(
                        audit_repo=audit_repo,
                        phone=event.operator_phone,
                        now_value=now_value,
                        window_minutes=settings_value.unauthorized_rejection_window_minutes,
                    ):
                        try:
                            await whatsapp_client_value.send_text(
                                to_phone=event.operator_phone,
                                message=settings_value.unauthorized_rejection_message,
                            )
                        except Exception as exc:
                            await audit_repo.log(
                                actor="system",
                                action="unauthorized_rejection_failed",
                                operator_phone=event.operator_phone,
                                metadata={"wamid": event.wamid, "error": str(exc)},
                            )
                        else:
                            await audit_repo.log(
                                actor="system",
                                action="unauthorized_rejection_sent",
                                operator_phone=event.operator_phone,
                                metadata={"wamid": event.wamid},
                            )
                    continue

                payload_obj = InboundTaskPayload(
                    wamid=event.wamid,
                    operator_phone=event.operator_phone,
                    message_timestamp=event.timestamp.isoformat() if event.timestamp else None,
                    raw_message=event.raw_message,
                )
                try:
                    await task_enqueuer_value.enqueue_inbound(payload_obj)
                except Exception as exc:
                    logger.exception(
                        "Inbound enqueue/processing failed (wamid=%s, operator_phone=%s, mode=%s)",
                        event.wamid,
                        event.operator_phone,
                        settings_value.tasks_mode,
                    )
                    async with session_scope(session_factory) as error_session:
                        await AuditEventRepository(error_session).log(
                            actor="system",
                            action="inbound_enqueue_failed",
                            operator_phone=event.operator_phone,
                            metadata={
                                "wamid": event.wamid,
                                "tasks_mode": settings_value.tasks_mode,
                                "error": str(exc),
                            },
                        )
                    if settings_value.tasks_mode == "cloud":
                        raise
                    raise HTTPException(
                        status_code=500,
                        detail="Inbound processing failed",
                    ) from exc
                enqueued_count += 1

            return {"status": "accepted", "received": len(events), "enqueued": enqueued_count}

    if app.state.worker_enabled:

        @app.post("/tasks/process-message")
        async def process_message_task(
            request: Request,
            payload: InboundTaskPayload,
            task_authorizer_value: TaskAuthorizerDep,
            process_inbound_value: ProcessInboundDep,
        ) -> dict[str, str]:
            authorization_header = request.headers.get("Authorization")
            if not await task_authorizer_value.is_authorized(authorization_header):
                raise HTTPException(status_code=401, detail="Unauthorized caller")

            result = await process_inbound_value(payload)
            return {"status": result.status}

    return app


app = create_app()
