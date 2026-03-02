import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.ad_generation import (
    NanoBananaAdGenerationService,
    NoopAdGenerationService,
)
from adv_assistant.config import Settings
from adv_assistant.db.base import utcnow
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
    verify_x_hub_signature,
)
from adv_assistant.llm_gateway import NoopLLMGateway, OpenAILLMGateway
from adv_assistant.media_ingest import (
    DefaultOperatorPhotoIngestor,
    NoopWhatsAppMediaClient,
    OperatorPhotoIngestor,
    WhatsAppMediaClient,
)
from adv_assistant.media_store import GCSMediaStore, MediaStore, NoopMediaStore
from adv_assistant.pipeline import InboundTaskProcessor
from adv_assistant.tasks_auth import (
    OidcTaskRequestAuthorizer,
    RejectAllTaskRequestAuthorizer,
    TaskRequestAuthorizer,
)
from adv_assistant.tasks_queue import (
    CloudTasksEnqueuer,
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
            ),
            NoopProductLookupProvider("ean_fallback"),
            NoopProductLookupProvider("web_search"),
        ]
    )


def _build_ad_generation_service(settings: Settings):
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
    raise RuntimeError(f"Unsupported MEDIA_STORE_MODE='{settings.media_store_mode}'")


def _build_whatsapp_media_client(settings: Settings) -> WhatsAppMediaClient:
    if settings.whatsapp_access_token:
        return MetaWhatsAppMediaClient(
            access_token=settings.whatsapp_access_token,
            graph_api_version=settings.whatsapp_graph_api_version,
            timeout_seconds=settings.whatsapp_media_timeout_seconds,
        )
    return NoopWhatsAppMediaClient()


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
    if not settings.enrichment_enabled:
        return

    required_columns = {
        "enriched_brand",
        "enriched_category",
        "enriched_description",
        "enriched_image_url",
        "enrichment_source",
        "enrichment_unavailable_notified_at",
    }

    async with engine.begin() as connection:
        table_names, ad_draft_columns = await connection.run_sync(_inspect_schema)

    if "ad_draft" not in table_names:
        raise RuntimeError(
            "Database schema is not initialized (missing 'ad_draft'). "
            "Run `uv run alembic upgrade head`."
        )

    missing_columns = sorted(required_columns - ad_draft_columns)
    if missing_columns:
        raise RuntimeError(
            "Database schema is behind application code. Missing columns in 'ad_draft': "
            f"{', '.join(missing_columns)}. Run `uv run alembic upgrade head`."
        )


def _inspect_schema(sync_connection) -> tuple[set[str], set[str]]:
    schema_inspector = inspect(sync_connection)
    table_names = set(schema_inspector.get_table_names())
    ad_draft_columns: set[str] = set()
    if "ad_draft" in table_names:
        ad_draft_columns = {column["name"] for column in schema_inspector.get_columns("ad_draft")}
    return table_names, ad_draft_columns


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
        )
    else:
        llm_gateway = NoopLLMGateway()

    whatsapp_client = _build_whatsapp_client(current_settings)
    whatsapp_media_client = _build_whatsapp_media_client(current_settings)
    enrichment_service = _build_enrichment_service(current_settings)
    ad_generation_service = _build_ad_generation_service(current_settings)
    media_store = _build_media_store(current_settings)
    operator_photo_ingestor = _build_operator_photo_ingestor(
        media_store=media_store,
        whatsapp_media_client=whatsapp_media_client,
    )
    task_processor = InboundTaskProcessor(
        session_factory,
        llm_gateway=llm_gateway,
        enrichment_service=enrichment_service,
        ad_generation_service=ad_generation_service,
        render_width=current_settings.ad_render_width,
        render_height=current_settings.ad_render_height,
        operator_photo_ingestor=operator_photo_ingestor,
    )

    async def process_and_maybe_send_reply(payload: InboundTaskPayload):
        result = await task_processor.process(payload)
        if (
            (result.generated_image_url or result.reply_text)
            and not result.duplicate
            and not result.unauthorized_operator
        ):
            try:
                if result.generated_image_url:
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
                            },
                        )
                except Exception:
                    pass
        return result

    task_enqueuer = _build_task_enqueuer(
        settings=current_settings,
        process_callback=process_and_maybe_send_reply,
    )
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
            await ad_generation_service.close()
            await media_store.close()
            await ad_generation_service.close()
            await engine.dispose()

    app = FastAPI(title=current_settings.app_name, lifespan=lifespan)

    app.state.settings = current_settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.task_processor = task_processor
    app.state.task_enqueuer = task_enqueuer
    app.state.task_authorizer = task_authorizer
    app.state.whatsapp_client = whatsapp_client
    app.state.ad_generation_service = ad_generation_service
    app.state.whatsapp_media_client = whatsapp_media_client
    app.state.media_store = media_store
    app.state.process_and_maybe_send_reply = process_and_maybe_send_reply

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

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
