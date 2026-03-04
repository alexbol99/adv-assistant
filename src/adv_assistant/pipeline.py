import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant.ad_generation import (
    AdGenerationError,
    AdGenerationService,
    GenerationDraftInput,
    GenerationMode,
    NanoBananaJobStatus,
    NoopAdGenerationService,
)
from adv_assistant.cms_cityscreen import CMSPublishError, CMSPublisher, NoopCMSPublisher
from adv_assistant.db.base import utcnow
from adv_assistant.db.enums import AdDraftStatus
from adv_assistant.db.models import AdDraft
from adv_assistant.db.repositories import (
    AdDraftRepository,
    AuditEventRepository,
    ConversationSessionRepository,
    OperatorRepository,
    ProcessedInboundMessageRepository,
    PublishedAdRepository,
)
from adv_assistant.db.session import session_scope
from adv_assistant.enrichment import (
    EnrichedProduct,
    EnrichmentService,
    NoopEnrichmentService,
    extract_ean_from_text,
)
from adv_assistant.llm_gateway import (
    BUTTON_CANCEL_PUBLISH,
    BUTTON_CANCEL_DELETE_ALL,
    BUTTON_CONFIRM_DELETE_ALL,
    BUTTON_CONFIRM_PUBLISH,
    Intent,
    LLMGateway,
    LLMGatewayError,
    LLMSchemaError,
    NoopLLMGateway,
    extract_button_payload_id,
    sanitize_user_text,
)
from adv_assistant.media_ingest import (
    MediaIngestError,
    NoopOperatorPhotoIngestor,
    OperatorPhotoIngestor,
)
from adv_assistant.tasks_queue import InboundTaskPayload

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ProcessInboundResult:
    duplicate: bool
    unauthorized_operator: bool = False
    session_created: bool = False
    draft_created: bool = False
    llm_used: bool = False
    intent: str | None = None
    deterministic_action: str | None = None
    reply_text: str | None = None
    generated_image_url: str | None = None
    publish_buttons_prompt: str | None = None

    @property
    def status(self) -> str:
        if self.duplicate:
            return "duplicate_skipped"
        if self.unauthorized_operator:
            return "unauthorized_operator"
        return "processed"


def _extract_text(raw_message: dict[str, Any]) -> str | None:
    if raw_message.get("type") != "text":
        return None
    text_payload = raw_message.get("text", {})
    body = text_payload.get("body")
    if not isinstance(body, str):
        return None
    stripped = body.strip()
    return stripped or None


def _extract_image_media_id(raw_message: dict[str, Any]) -> str | None:
    if raw_message.get("type") != "image":
        return None
    image_payload = raw_message.get("image")
    if not isinstance(image_payload, dict):
        return None
    media_id = image_payload.get("id")
    if not isinstance(media_id, str):
        return None
    stripped = media_id.strip()
    return stripped or None


class InboundTaskProcessor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        llm_gateway: LLMGateway | None = None,
        enrichment_service: EnrichmentService | None = None,
        ad_generation_service: AdGenerationService | None = None,
        render_width: int = 1920,
        render_height: int = 1080,
        operator_photo_ingestor: OperatorPhotoIngestor | None = None,
        cms_publisher: CMSPublisher | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._llm_gateway = llm_gateway or NoopLLMGateway()
        self._enrichment_service = enrichment_service or NoopEnrichmentService()
        self._ad_generation_service = ad_generation_service or NoopAdGenerationService()
        self._render_width = render_width
        self._render_height = render_height
        self._operator_photo_ingestor = operator_photo_ingestor or NoopOperatorPhotoIngestor()
        self._cms_publisher = cms_publisher or NoopCMSPublisher()

    async def process(self, payload: InboundTaskPayload) -> ProcessInboundResult:
        async with session_scope(self._session_factory) as session:
            operator_repo = OperatorRepository(session)
            session_repo = ConversationSessionRepository(session)
            draft_repo = AdDraftRepository(session)
            published_repo = PublishedAdRepository(session)
            processed_repo = ProcessedInboundMessageRepository(session)
            audit_repo = AuditEventRepository(session)

            inserted = await processed_repo.mark_processed(
                wamid=payload.wamid,
                operator_phone=payload.operator_phone,
            )
            if not inserted:
                return ProcessInboundResult(duplicate=True)

            operator = await operator_repo.get_by_phone(payload.operator_phone)
            if operator is None or not operator.active:
                await audit_repo.log(
                    actor="system",
                    action="inbound_unauthorized_operator_skipped",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
                return ProcessInboundResult(duplicate=False, unauthorized_operator=True)

            now = utcnow()
            session_obj = await session_repo.get_by_operator_phone(payload.operator_phone)
            session_created = session_obj is None

            history: list[dict[str, str]] = []
            current_draft_id: uuid.UUID | None = None
            if session_obj is not None:
                history = list(session_obj.history)
                current_draft_id = session_obj.current_draft_id

            draft_created = False
            if current_draft_id is not None:
                existing_draft = await draft_repo.get_by_id(current_draft_id)
                if (
                    existing_draft is None
                    or existing_draft.operator_phone != payload.operator_phone
                ):
                    current_draft_id = None
                    await audit_repo.log(
                        actor="system",
                        action="session_draft_owner_mismatch",
                        operator_phone=payload.operator_phone,
                        metadata={"wamid": payload.wamid},
                    )

            if current_draft_id is None:
                created_draft = await draft_repo.create(
                    operator_phone=payload.operator_phone,
                    status=AdDraftStatus.DRAFT,
                )
                current_draft_id = created_draft.id
                draft_created = True

            current_draft = await draft_repo.get_by_id(current_draft_id)
            if current_draft is None:
                raise RuntimeError("current draft was not created")

            incoming_text = _extract_text(payload.raw_message)
            incoming_image_media_id = _extract_image_media_id(payload.raw_message)
            button_payload_id = extract_button_payload_id(payload.raw_message)
            llm_used = False
            intent_value: str | None = None
            deterministic_action: str | None = None
            reply_text: str | None = None
            generated_image_url: str | None = None
            publish_buttons_prompt: str | None = None

            if button_payload_id:
                history.append(
                    {
                        "role": "user",
                        "text": f"[button:{button_payload_id}]",
                        "wamid": payload.wamid,
                    }
                )
                if button_payload_id == BUTTON_CONFIRM_PUBLISH:
                    deterministic_action = "confirm_publish"
                    intent_value = deterministic_action
                    current_draft, reply_text = await self._confirm_publish_to_cms(
                        payload=payload,
                        draft_repo=draft_repo,
                        published_repo=published_repo,
                        audit_repo=audit_repo,
                        current_draft=current_draft,
                        language=operator.language,
                    )
                else:
                    deterministic_action, reply_text = _resolve_button_action(button_payload_id)
                    intent_value = deterministic_action
                await audit_repo.log(
                    actor="system",
                    action="button_callback_resolved",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid, "button_id": button_payload_id},
                )
            elif incoming_image_media_id is not None:
                history.append(
                    {
                        "role": "user",
                        "text": "[image]",
                        "wamid": payload.wamid,
                    }
                )
                deterministic_action = "operator_photo_ingest"
                intent_value = deterministic_action
                current_draft, reply_text = await self._process_operator_photo_message(
                    payload=payload,
                    draft_repo=draft_repo,
                    audit_repo=audit_repo,
                    current_draft=current_draft,
                    language=operator.language,
                    media_id=incoming_image_media_id,
                )
            elif incoming_text is not None:
                sanitized_text = sanitize_user_text(incoming_text, max_chars=2000)
                history.append(
                    {
                        "role": "user",
                        "text": sanitized_text,
                        "wamid": payload.wamid,
                    }
                )
                try:
                    classification = await self._llm_gateway.classify_intent(
                        message_text=sanitized_text,
                        language=operator.language,
                        history=history,
                    )
                    llm_used = self._llm_gateway.uses_external_llm or llm_used
                    intent_value = classification.intent.value

                    extracted_fields = None
                    enrichment_notice: str | None = None
                    detected_ean = extract_ean_from_text(sanitized_text)
                    if classification.intent in {
                        Intent.CREATE_AD,
                        Intent.REGENERATE_WITH_REFERENCE,
                        Intent.REGENERATE_FROM_SCRATCH,
                    }:
                        extracted_fields = await self._llm_gateway.extract_ad_fields(
                            message_text=sanitized_text,
                            language=operator.language,
                            history=history,
                        )
                        llm_used = self._llm_gateway.uses_external_llm or llm_used
                        update_fields = extracted_fields.to_draft_update_fields()
                        if update_fields:
                            updated_draft = await draft_repo.update_for_operator_with_version(
                                draft_id=current_draft.id,
                                operator_phone=payload.operator_phone,
                                expected_version=current_draft.version,
                                **update_fields,
                            )
                            if updated_draft is None:
                                reply_text = (
                                    "This draft was already changed. Please refresh and try again."
                                )
                                await audit_repo.log(
                                    actor="system",
                                    action="draft_stale_write_detected",
                                    operator_phone=payload.operator_phone,
                                    metadata={"wamid": payload.wamid},
                                )
                            else:
                                current_draft = updated_draft
                        current_draft, enrichment_notice = await self._enrich_current_draft(
                            payload=payload,
                            draft_repo=draft_repo,
                            audit_repo=audit_repo,
                            current_draft=current_draft,
                            language=operator.language,
                            detected_ean=detected_ean,
                            allow_existing_draft_ean=True,
                        )
                    elif classification.intent == Intent.UNKNOWN and detected_ean is not None:
                        current_draft, enrichment_notice = await self._enrich_current_draft(
                            payload=payload,
                            draft_repo=draft_repo,
                            audit_repo=audit_repo,
                            current_draft=current_draft,
                            language=operator.language,
                            detected_ean=detected_ean,
                            allow_existing_draft_ean=False,
                        )
                        reply_text = _build_barcode_lookup_reply(
                            draft=current_draft,
                            ean=detected_ean,
                            language=operator.language,
                            unavailable_notice=enrichment_notice,
                        )
                        enrichment_notice = None

                    if (
                        reply_text is None
                        and classification.intent
                        in {
                            Intent.CREATE_AD,
                            Intent.REGENERATE_WITH_REFERENCE,
                            Intent.REGENERATE_FROM_SCRATCH,
                        }
                        and self._ad_generation_service.enabled
                    ):
                        mode = _generation_mode_for_intent(classification.intent)
                        if _is_ready_for_generation(current_draft):
                            try:
                                submission = await self._ad_generation_service.submit_for_draft(
                                    draft=_to_generation_draft_input(
                                        draft=current_draft,
                                        operator_phone=payload.operator_phone,
                                        language=operator.language,
                                    ),
                                    mode=mode,
                                    instruction_text=sanitized_text,
                                    wamid=payload.wamid,
                                    width=self._render_width,
                                    height=self._render_height,
                                )
                                updated_draft = await draft_repo.update_for_operator_with_version(
                                    draft_id=current_draft.id,
                                    operator_phone=payload.operator_phone,
                                    expected_version=current_draft.version,
                                    status=AdDraftStatus.GENERATING,
                                    generation_job_id=submission.job_id,
                                )
                                if updated_draft is None:
                                    reply_text = (
                                        "This draft was already changed. "
                                        "Please refresh and try again."
                                    )
                                    await audit_repo.log(
                                        actor="system",
                                        action="draft_stale_write_detected",
                                        operator_phone=payload.operator_phone,
                                        metadata={"wamid": payload.wamid},
                                    )
                                else:
                                    current_draft = updated_draft
                                    await audit_repo.log(
                                        actor="system",
                                        action="generation_job_submitted",
                                        operator_phone=payload.operator_phone,
                                        metadata={
                                            "wamid": payload.wamid,
                                            "draft_id": str(current_draft.id),
                                            "job_id": submission.job_id,
                                            "mode": submission.mode.value,
                                            "idempotency_key": submission.idempotency_key,
                                        },
                                    )
                                    logger.info(
                                        "Generation job submitted "
                                        "(wamid=%s, operator_phone=%s, draft_id=%s, "
                                        "job_id=%s, mode=%s)",
                                        payload.wamid,
                                        payload.operator_phone,
                                        current_draft.id,
                                        submission.job_id,
                                        submission.mode.value,
                                    )
                                    poll_result = (
                                        await self._ad_generation_service.wait_for_completion(
                                            job_id=submission.job_id
                                        )
                                    )
                                    if (
                                        poll_result.status == NanoBananaJobStatus.COMPLETED
                                        and poll_result.output_image_url
                                    ):
                                        completed_draft = (
                                            await draft_repo.update_for_operator_with_version(
                                                draft_id=current_draft.id,
                                                operator_phone=payload.operator_phone,
                                                expected_version=current_draft.version,
                                                status=AdDraftStatus.PREVIEW_READY,
                                                rendered_image_url=poll_result.output_image_url,
                                                preview_reference_url=poll_result.output_image_url,
                                            )
                                        )
                                        if completed_draft is None:
                                            reply_text = (
                                                "This draft was already changed. "
                                                "Please refresh and try again."
                                            )
                                            await audit_repo.log(
                                                actor="system",
                                                action="draft_stale_write_detected",
                                                operator_phone=payload.operator_phone,
                                                metadata={"wamid": payload.wamid},
                                            )
                                        else:
                                            current_draft = completed_draft
                                            deterministic_action = "generation_completed"
                                            await audit_repo.log(
                                                actor="system",
                                                action="generation_completed",
                                                operator_phone=payload.operator_phone,
                                                metadata={
                                                    "wamid": payload.wamid,
                                                    "draft_id": str(current_draft.id),
                                                    "job_id": submission.job_id,
                                                    "output_image_url": (
                                                        poll_result.output_image_url
                                                    ),
                                                },
                                            )
                                            logger.info(
                                                "Generation job completed "
                                                "(wamid=%s, operator_phone=%s, draft_id=%s, "
                                                "job_id=%s, output=%s)",
                                                payload.wamid,
                                                payload.operator_phone,
                                                current_draft.id,
                                                submission.job_id,
                                                poll_result.output_image_url,
                                            )
                                            generated_image_url = poll_result.output_image_url
                                            reply_text = _generation_completed_reply(
                                                operator.language,
                                            )
                                            publish_buttons_prompt = _publish_buttons_prompt(
                                                operator.language
                                            )
                                    else:
                                        failed_draft = (
                                            await draft_repo.update_for_operator_with_version(
                                                draft_id=current_draft.id,
                                                operator_phone=payload.operator_phone,
                                                expected_version=current_draft.version,
                                                status=AdDraftStatus.DRAFT,
                                            )
                                        )
                                        if failed_draft is not None:
                                            current_draft = failed_draft
                                        deterministic_action = "generation_failed"
                                        await audit_repo.log(
                                            actor="system",
                                            action="generation_failed",
                                            operator_phone=payload.operator_phone,
                                            metadata={
                                                "wamid": payload.wamid,
                                                "draft_id": str(current_draft.id),
                                                "job_id": submission.job_id,
                                                "status": poll_result.status.value,
                                                "error_code": poll_result.error_code,
                                                "error_message": poll_result.error_message,
                                            },
                                        )
                                        logger.warning(
                                            "Generation job failed status "
                                            "(wamid=%s, operator_phone=%s, draft_id=%s, "
                                            "job_id=%s, status=%s, error_code=%s, "
                                            "error_message=%s)",
                                            payload.wamid,
                                            payload.operator_phone,
                                            current_draft.id,
                                            submission.job_id,
                                            poll_result.status.value,
                                            poll_result.error_code,
                                            poll_result.error_message,
                                        )
                                        reply_text = _generation_failed_reply(operator.language)
                            except AdGenerationError as exc:
                                if current_draft.status == AdDraftStatus.GENERATING:
                                    reverted_draft = (
                                        await draft_repo.update_for_operator_with_version(
                                            draft_id=current_draft.id,
                                            operator_phone=payload.operator_phone,
                                            expected_version=current_draft.version,
                                            status=AdDraftStatus.DRAFT,
                                        )
                                    )
                                    if reverted_draft is not None:
                                        current_draft = reverted_draft
                                reply_text = _generation_failed_reply(operator.language)
                                await audit_repo.log(
                                    actor="system",
                                    action="generation_flow_failed",
                                    operator_phone=payload.operator_phone,
                                    metadata={
                                        "wamid": payload.wamid,
                                        "draft_id": str(current_draft.id),
                                        "mode": mode.value,
                                        "error": str(exc),
                                    },
                                )
                                logger.exception(
                                    "Generation flow failed "
                                    "(wamid=%s, operator_phone=%s, draft_id=%s, mode=%s)",
                                    payload.wamid,
                                    payload.operator_phone,
                                    current_draft.id,
                                    mode.value,
                                )

                    if reply_text is None:
                        if classification.intent == Intent.PUBLISH_AD:
                            reply_text = _publish_confirmation_prompt(operator.language)
                            publish_buttons_prompt = _publish_buttons_prompt(operator.language)
                        elif classification.intent == Intent.DELETE_ALL:
                            reply_text = (
                                "Please use the confirmation button to continue with this action."
                            )
                        else:
                            reply = await self._llm_gateway.generate_reply(
                                intent=classification.intent,
                                message_text=sanitized_text,
                                language=operator.language,
                                extracted_fields=extracted_fields,
                            )
                            llm_used = self._llm_gateway.uses_external_llm or llm_used
                            reply_text = reply.reply_text
                    if enrichment_notice:
                        if reply_text:
                            reply_text = f"{reply_text}\n\n{enrichment_notice}"
                        else:
                            reply_text = enrichment_notice
                except LLMSchemaError:
                    reply_text = (
                        "I could not safely parse your request. "
                        "Please rephrase in one short message."
                    )
                    await audit_repo.log(
                        actor="system",
                        action="llm_schema_mismatch_fallback",
                        operator_phone=payload.operator_phone,
                        metadata={"wamid": payload.wamid},
                    )
                except LLMGatewayError:
                    reply_text = "Temporary AI service issue. Please try again in a moment."
                    await audit_repo.log(
                        actor="system",
                        action="llm_gateway_failure_fallback",
                        operator_phone=payload.operator_phone,
                        metadata={"wamid": payload.wamid},
                    )
            else:
                reply_text = (
                    "Unsupported message type. Please send text, image, "
                    "or use confirmation buttons."
                )

            if reply_text is not None:
                history.append(
                    {
                        "role": "assistant",
                        "text": reply_text,
                        "wamid": payload.wamid,
                    }
                )

            await session_repo.create_or_update(
                operator_phone=payload.operator_phone,
                language=operator.language if session_created else None,
                history=history,
                current_draft_id=current_draft.id,
                last_active_at=now,
            )

            if session_created:
                await audit_repo.log(
                    actor="system",
                    action="conversation_session_created",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
            if draft_created:
                await audit_repo.log(
                    actor="system",
                    action="operator_draft_created",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid, "draft_id": str(current_draft_id)},
                )

            await audit_repo.log(
                actor="system",
                action="inbound_message_processed",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "session_created": session_created,
                    "draft_created": draft_created,
                    "intent": intent_value,
                    "deterministic_action": deterministic_action,
                    "llm_used": llm_used,
                },
            )
            return ProcessInboundResult(
                duplicate=False,
                unauthorized_operator=False,
                session_created=session_created,
                draft_created=draft_created,
                llm_used=llm_used,
                intent=intent_value,
                deterministic_action=deterministic_action,
                reply_text=reply_text,
                generated_image_url=generated_image_url,
                publish_buttons_prompt=publish_buttons_prompt,
            )

    async def _confirm_publish_to_cms(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        published_repo: PublishedAdRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        language: str,
    ) -> tuple[AdDraft, str]:
        if current_draft.rendered_image_url is None:
            if language.lower() == "he":
                return current_draft, "אין כרגע תמונת תצוגה מוכנה לפרסום."
            return current_draft, "There is no generated preview image ready for publishing."

        if not self._cms_publisher.enabled:
            if language.lower() == "he":
                return current_draft, "הפרסום ל-CMS לא מוגדר כרגע במערכת."
            return current_draft, "CMS publishing is not configured yet."

        title = current_draft.product_name or f"draft-{current_draft.id}"
        logger.info(
            "CMS publish requested (wamid=%s, operator_phone=%s, draft_id=%s, image_url=%s)",
            payload.wamid,
            payload.operator_phone,
            current_draft.id,
            current_draft.rendered_image_url,
        )
        print(
            "[CMS] publish requested "
            f"wamid={payload.wamid} operator_phone={payload.operator_phone} "
            f"draft_id={current_draft.id}",
            flush=True,
        )

        try:
            publish_result = await self._cms_publisher.publish_generated_image(
                image_url=current_draft.rendered_image_url,
                title=title,
            )
            cms_id = str(publish_result.advertisement_id)
            existing = await published_repo.get_by_cms_id(cms_id)
            if existing is None:
                await published_repo.create(cms_id=cms_id, ad_draft_id=current_draft.id)

            updated_draft = await draft_repo.update_for_operator_with_version(
                draft_id=current_draft.id,
                operator_phone=payload.operator_phone,
                expected_version=current_draft.version,
                status=AdDraftStatus.PUBLISHED,
            )
            if updated_draft is None:
                await audit_repo.log(
                    actor="system",
                    action="draft_stale_write_detected",
                    operator_phone=payload.operator_phone,
                    metadata={
                        "wamid": payload.wamid,
                        "draft_id": str(current_draft.id),
                        "cms_id": cms_id,
                        "expected_version": current_draft.version,
                        "context": "cms_publish_status_update",
                    },
                )
                logger.warning(
                    "Stale draft write detected after CMS publish "
                    "(wamid=%s, operator_phone=%s, draft_id=%s, expected_version=%s)",
                    payload.wamid,
                    payload.operator_phone,
                    current_draft.id,
                    current_draft.version,
                )
                if language.lower() == "he":
                    return (
                        current_draft,
                        "הפרסום בוצע, אך התרחשה שגיאה זמנית בעדכון סטטוס הטיוטה. "
                        "אנא רענן את השיחה ונסה שוב.",
                    )
                return (
                    current_draft,
                    "Your ad was published, but there was a temporary issue updating "
                    "the draft status. Please refresh and try again.",
                )
            current_draft = updated_draft

            await audit_repo.log(
                actor="system",
                action="publish_ad_success",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "cms_id": cms_id,
                    "file_id": publish_result.file_id,
                    "advertisement_id": publish_result.advertisement_id,
                    "slot_id": publish_result.slot_id,
                },
            )
            logger.info(
                "CMS publish succeeded (wamid=%s, operator_phone=%s, draft_id=%s, "
                "cms_id=%s, slot_id=%s)",
                payload.wamid,
                payload.operator_phone,
                current_draft.id,
                cms_id,
                publish_result.slot_id,
            )
            print(
                "[CMS] publish succeeded "
                f"draft_id={current_draft.id} cms_id={cms_id} slot_id={publish_result.slot_id}",
                flush=True,
            )
            if language.lower() == "he":
                return current_draft, "הפרסום הצליח. המודעה נוספה לפלייליסט."
            return current_draft, "Publishing succeeded. Your ad was added to the playlist."
        except CMSPublishError as exc:
            await audit_repo.log(
                actor="system",
                action="publish_ad_failed",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "error": str(exc),
                },
            )
            logger.exception(
                "CMS publish failed (wamid=%s, operator_phone=%s, draft_id=%s)",
                payload.wamid,
                payload.operator_phone,
                current_draft.id,
            )
            print(
                "[CMS] publish failed "
                f"draft_id={current_draft.id} error={exc}",
                flush=True,
            )
            if language.lower() == "he":
                return current_draft, "הפרסום נכשל כרגע. נסה שוב בעוד רגע."
            return current_draft, "Publishing failed right now. Please try again in a moment."

    async def _process_operator_photo_message(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        language: str,
        media_id: str,
    ) -> tuple[AdDraft, str]:
        try:
            ingested_photo = await self._operator_photo_ingestor.ingest_whatsapp_image(
                media_id=media_id
            )
        except MediaIngestError as exc:
            await audit_repo.log(
                actor="system",
                action="operator_photo_ingest_failed",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid, "media_id": media_id, "error": str(exc)},
            )
            return (
                current_draft,
                "I could not process your photo right now. Please try sending it again.",
            )

        updated_draft = await draft_repo.update_for_operator_with_version(
            draft_id=current_draft.id,
            operator_phone=payload.operator_phone,
            expected_version=current_draft.version,
            photo_url=ingested_photo.public_url,
        )
        if updated_draft is None:
            await audit_repo.log(
                actor="system",
                action="draft_stale_write_detected",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid, "media_id": media_id},
            )
            return (
                current_draft,
                "This draft was already changed. Please refresh and try again.",
            )
        current_draft = updated_draft

        detected_ean: str | None = None
        try:
            detected_ean = await self._enrichment_service.decode_ean_from_image(
                ingested_photo.content
            )
        except Exception as exc:
            await audit_repo.log(
                actor="system",
                action="barcode_decode_failed",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid, "media_id": media_id, "error": str(exc)},
            )

        enrichment_notice: str | None = None
        try:
            current_draft, enrichment_notice = await self._enrich_current_draft(
                payload=payload,
                draft_repo=draft_repo,
                audit_repo=audit_repo,
                current_draft=current_draft,
                language=language,
                detected_ean=detected_ean,
                allow_existing_draft_ean=False,
            )
        except Exception as exc:
            await audit_repo.log(
                actor="system",
                action="photo_enrichment_failed",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid, "media_id": media_id, "error": str(exc)},
            )

        await audit_repo.log(
            actor="system",
            action="operator_photo_ingested",
            operator_phone=payload.operator_phone,
            metadata={
                "wamid": payload.wamid,
                "media_id": media_id,
                "media_object_name": ingested_photo.object_name,
                "photo_url": ingested_photo.public_url,
                "detected_ean": detected_ean,
            },
        )

        reply_text = _build_photo_ingest_reply(
            draft=current_draft,
            language=language,
            detected_ean=detected_ean,
        )
        if enrichment_notice:
            reply_text = f"{reply_text}\n\n{enrichment_notice}"
        return current_draft, reply_text

    async def _enrich_current_draft(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        language: str,
        detected_ean: str | None,
        allow_existing_draft_ean: bool,
    ) -> tuple[AdDraft, str | None]:
        ean = detected_ean
        if ean is None and allow_existing_draft_ean:
            ean = current_draft.ean
        if ean is None:
            return current_draft, None

        if current_draft.ean is None:
            updated_with_ean = await draft_repo.update_for_operator_with_version(
                draft_id=current_draft.id,
                operator_phone=payload.operator_phone,
                expected_version=current_draft.version,
                ean=ean,
            )
            if updated_with_ean is not None:
                current_draft = updated_with_ean

        enriched = await self._enrichment_service.enrich_by_ean(ean=ean, language=language)
        if enriched is None:
            if current_draft.enrichment_unavailable_notified_at is not None:
                return current_draft, None

            updated_draft = await draft_repo.update_for_operator_with_version(
                draft_id=current_draft.id,
                operator_phone=payload.operator_phone,
                expected_version=current_draft.version,
                enrichment_source="none",
                enrichment_unavailable_notified_at=utcnow(),
            )
            if updated_draft is not None:
                current_draft = updated_draft
            await audit_repo.log(
                actor="system",
                action="enrichment_unavailable_notified",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid, "ean": ean},
            )
            return (
                current_draft,
                "I could not find additional product details for this barcode yet. "
                "Continuing with your provided information.",
            )

        update_fields = self._build_enrichment_update_fields(current_draft, enriched)
        if not update_fields:
            return current_draft, None

        updated_draft = await draft_repo.update_for_operator_with_version(
            draft_id=current_draft.id,
            operator_phone=payload.operator_phone,
            expected_version=current_draft.version,
            **update_fields,
        )
        if updated_draft is not None:
            current_draft = updated_draft
            await audit_repo.log(
                actor="system",
                action="enrichment_applied",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "ean": ean,
                    "source": enriched.source,
                    "updated_fields": sorted(update_fields.keys()),
                },
            )
        return current_draft, None

    def _build_enrichment_update_fields(
        self,
        current_draft: AdDraft,
        enriched: EnrichedProduct,
    ) -> dict[str, Any]:
        update_fields: dict[str, Any] = {
            "enrichment_source": enriched.source,
            "enriched_brand": _truncate(enriched.brand, 120),
            "enriched_category": _truncate(enriched.category, 120),
            "enriched_description": _truncate(enriched.description, 500),
            "enriched_image_url": _truncate(enriched.image_url, 2000),
            "enrichment_unavailable_notified_at": None,
        }

        if current_draft.product_name is None and enriched.product_name is not None:
            update_fields["product_name"] = _truncate(enriched.product_name, 120)
        if current_draft.promo_text is None and enriched.description is not None:
            update_fields["promo_text"] = _truncate(enriched.description, 240)
        if current_draft.photo_url is None and enriched.image_url is not None:
            update_fields["photo_url"] = _truncate(enriched.image_url, 2000)

        cleaned: dict[str, Any] = {}
        for key, value in update_fields.items():
            if value is None and key not in {
                "enrichment_unavailable_notified_at",
                "enrichment_source",
            }:
                continue
            if getattr(current_draft, key, None) == value:
                continue
            cleaned[key] = value
        return cleaned


def _resolve_button_action(button_id: str) -> tuple[str | None, str]:
    if button_id == BUTTON_CONFIRM_PUBLISH:
        return "confirm_publish", "Publishing confirmed."
    if button_id == BUTTON_CANCEL_PUBLISH:
        return "cancel_publish", "Publishing canceled."
    if button_id == BUTTON_CONFIRM_DELETE_ALL:
        return (
            "confirm_delete_all",
            "Delete-all confirmed. Deletion flow will be handled in the next phase.",
        )
    if button_id == BUTTON_CANCEL_DELETE_ALL:
        return "cancel_delete_all", "Delete-all canceled."
    return None, "Unknown confirmation button."


def _truncate(value: str | None, max_length: int) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    return stripped[:max_length]


def _build_barcode_lookup_reply(
    *,
    draft: AdDraft,
    ean: str,
    language: str,
    unavailable_notice: str | None,
) -> str:
    product_name = draft.product_name or draft.enriched_brand
    if product_name:
        if language.lower() == "he":
            return (
                f"זיהיתי את הברקוד {ean}. "
                f"המוצר שנמצא: {product_name}. "
                "אפשר להמשיך עכשיו ליצירת מודעה."
            )
        return (
            f"I detected barcode {ean}. "
            f"Found product: {product_name}. "
            "We can continue now to ad creation."
        )
    if unavailable_notice:
        return unavailable_notice
    if language.lower() == "he":
        return f"קיבלתי את הברקוד {ean}, אבל לא מצאתי פרטי מוצר כרגע."
    return f"I received barcode {ean}, but I could not find product details yet."


def _generation_mode_for_intent(intent: Intent) -> GenerationMode:
    if intent == Intent.REGENERATE_WITH_REFERENCE:
        return GenerationMode.REFERENCE
    return GenerationMode.FRESH


def _is_ready_for_generation(draft: AdDraft) -> bool:
    return draft.product_name is not None and draft.price is not None


def _to_generation_draft_input(
    *,
    draft: AdDraft,
    operator_phone: str,
    language: str,
) -> GenerationDraftInput:
    return GenerationDraftInput(
        draft_id=draft.id,
        operator_phone=operator_phone,
        language=language,
        product_name=draft.product_name,
        price=draft.price,
        currency=draft.currency,
        promo_text=draft.promo_text,
        ean=draft.ean,
        photo_url=draft.photo_url,
        enriched_brand=draft.enriched_brand,
        enriched_category=draft.enriched_category,
        enriched_description=draft.enriched_description,
        preview_reference_url=draft.preview_reference_url,
        rendered_image_url=draft.rendered_image_url,
    )


def _generation_completed_reply(language: str) -> str:
    if language.lower() == "he":
        return "התצוגה המקדימה מוכנה."
    return "Your ad preview is ready."


def _publish_confirmation_prompt(language: str) -> str:
    if language.lower() == "he":
        return "בחר האם לפרסם עכשיו ל-CMS או לבטל."
    return "Choose whether to publish to CMS now or cancel."


def _publish_buttons_prompt(language: str) -> str:
    if language.lower() == "he":
        return "לפרסם את המודעה הזו ל-CMS?"
    return "Publish this ad to CMS?"


def _generation_failed_reply(language: str) -> str:
    if language.lower() == "he":
        return "יש כרגע תקלה זמנית ביצירת המודעה. נסה שוב בעוד רגע."
    return "Temporary generation service issue. Please try again in a moment."


def _build_photo_ingest_reply(
    *,
    draft: AdDraft,
    language: str,
    detected_ean: str | None,
) -> str:
    product_name = draft.product_name or draft.enriched_brand
    if detected_ean and product_name:
        if language.lower() == "he":
            return f"קיבלתי את התמונה, זיהיתי ברקוד {detected_ean}, והמוצר הוא: {product_name}."
        return (
            f"I received your photo, detected barcode {detected_ean}, "
            f"and found product: {product_name}."
        )
    if detected_ean:
        if language.lower() == "he":
            return f"קיבלתי את התמונה וזיהיתי ברקוד {detected_ean}."
        return f"I received your photo and detected barcode {detected_ean}."
    if language.lower() == "he":
        return "קיבלתי את התמונה ושמרתי אותה בטיוטה."
    return "I received your photo and saved it to the current draft."
