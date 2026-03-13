import logging
import uuid
from collections.abc import Awaitable, Callable
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
from adv_assistant.cms_cityscreen import CMSPublisher, CMSPublishError, NoopCMSPublisher
from adv_assistant.db.base import utcnow
from adv_assistant.db.enums import (
    AdDraftStatus,
    AdRequestType,
    ClassificationStatus,
    PendingQuestionType,
)
from adv_assistant.db.models import AdDraft, ConversationSession, Operator
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
    BUTTON_CANCEL_DELETE_ALL,
    BUTTON_CANCEL_PUBLISH,
    BUTTON_CONFIRM_DELETE_ALL,
    BUTTON_CONFIRM_PUBLISH,
    ExtractedAdFields,
    Intent,
    IntentClassification,
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
from adv_assistant.whatsapp import NoopWhatsAppClient, WhatsAppClient

logger = logging.getLogger(__name__)

TraceSink = Callable[[str, str | None, dict[str, Any]], Awaitable[None]]

_ONBOARDING_STEP_AWAITING_NAME = "awaiting_name"
_ONBOARDING_STEP_AWAITING_LOGO = "awaiting_logo"

_PENDING_UPLOAD_LOGO = "logo"
_PENDING_FOLLOWUP_PRICE = "price"
_PENDING_FOLLOWUP_STORE_TYPE = "store_type"
_PENDING_FOLLOWUP_CREATIVE_GUIDANCE = "creative_guidance"
_PENDING_FOLLOWUP_REGENERATE_CONFIRMATION = "regenerate_confirmation"
_PENDING_FOLLOWUP_KEYS = {
    _PENDING_FOLLOWUP_PRICE,
    _PENDING_FOLLOWUP_STORE_TYPE,
    _PENDING_FOLLOWUP_CREATIVE_GUIDANCE,
}
_FOLLOWUP_INTERRUPT_INTENTS = {
    Intent.CREATE_AD,
    Intent.REGENERATE_WITH_REFERENCE,
    Intent.REGENERATE_FROM_SCRATCH,
    Intent.PUBLISH_AD,
    Intent.SET_LOGO,
    Intent.SET_BRANDING,
    Intent.DELETE_ALL,
    Intent.LIST_ADS,
    Intent.HELP,
}
_AD_INTENTS = {
    Intent.CREATE_AD,
    Intent.REGENERATE_WITH_REFERENCE,
    Intent.REGENERATE_FROM_SCRATCH,
}
_REQUEST_TYPE_SINGLE_KEYWORDS = {
    "single",
    "single product",
    "one product",
    "מוצר אחד",
    "מוצר בודד",
    "פריט אחד",
}
_REQUEST_TYPE_MULTI_KEYWORDS = {
    "multi",
    "multi product",
    "multiple products",
    "several products",
    "catalog",
    "כמה מוצרים",
    "מספר מוצרים",
    "כמה פריטים",
    "כמה מוצרים יחד",
    "קטלוג",
}
_REQUEST_TYPE_STORE_GENERAL_KEYWORDS = {
    "store general",
    "general store",
    "shop wide",
    "whole store",
    "entire store",
    "business ad",
    "store ad",
    "חנות",
    "החנות",
    "העסק",
    "מודעה לחנות",
    "מודעה כללית",
    "מבצעי החנות",
}


@dataclass(slots=True)
class RequestTypeDecision:
    request_type: AdRequestType
    resolved: bool
    extracted_fields: ExtractedAdFields | None = None


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


def _operator_needs_onboarding(operator: Operator) -> bool:
    """Return True if the operator has not completed first-time onboarding."""
    return operator.business_name is None or operator.logo_url is None


def _resolve_onboarding_step(operator: Operator) -> str:
    """Determine which onboarding step applies based on operator state."""
    if operator.business_name is None:
        return _ONBOARDING_STEP_AWAITING_NAME
    return _ONBOARDING_STEP_AWAITING_LOGO


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


def _normalized_casefold_text(value: str) -> str:
    return " ".join(value.split()).strip().casefold()


def _contains_any_keyword(message_text: str, keywords: set[str]) -> bool:
    normalized = _normalized_casefold_text(message_text)
    return any(keyword in normalized for keyword in keywords)


def _classification_prompt(language: str) -> str:
    if language.lower() == "he":
        return (
            "כדי להמשיך צריך להבין איזה סוג מודעה אתה רוצה: "
            "מוצר אחד, כמה מוצרים, או מודעה כללית לחנות? "
            "כתוב רק אחת מהאפשרויות."
        )
    return (
        "To continue, tell me which ad type you want: "
        "single product, multiple products, or a general store ad. "
        "Reply with one option only."
    )


def _request_type_resolved_reply(*, request_type: AdRequestType, language: str) -> str | None:
    if request_type == AdRequestType.MULTI_PRODUCT:
        if language.lower() == "he":
            return "הבנתי שמדובר במודעה לכמה מוצרים. הסיווג נשמר."
        return "Understood. This is a multi-product ad request. The classification was saved."
    if request_type == AdRequestType.STORE_GENERAL:
        if language.lower() == "he":
            return "הבנתי שמדובר במודעה כללית לחנות. הסיווג נשמר."
        return "Understood. This is a general store ad request. The classification was saved."
    return None


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
        whatsapp_client: WhatsAppClient | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._llm_gateway = llm_gateway or NoopLLMGateway()
        self._enrichment_service = enrichment_service or NoopEnrichmentService()
        self._ad_generation_service = ad_generation_service or NoopAdGenerationService()
        self._render_width = render_width
        self._render_height = render_height
        self._operator_photo_ingestor = operator_photo_ingestor or NoopOperatorPhotoIngestor()
        self._cms_publisher = cms_publisher or NoopCMSPublisher()
        self._whatsapp_client = whatsapp_client or NoopWhatsAppClient()

    async def process(self, payload: InboundTaskPayload) -> ProcessInboundResult:
        async with session_scope(self._session_factory) as session:
            operator_repo = OperatorRepository(session)
            session_repo = ConversationSessionRepository(session)
            draft_repo = AdDraftRepository(session)
            published_repo = PublishedAdRepository(session)
            processed_repo = ProcessedInboundMessageRepository(session)
            audit_repo = AuditEventRepository(session)
            trace_sink = self._build_trace_sink(audit_repo=audit_repo)
            self._set_provider_trace_context(
                self._llm_gateway,
                operator_phone=payload.operator_phone,
                wamid=payload.wamid,
                trace_sink=trace_sink,
            )
            self._set_provider_trace_context(
                self._ad_generation_service,
                operator_phone=payload.operator_phone,
                wamid=payload.wamid,
                trace_sink=trace_sink,
            )

            inserted = await processed_repo.mark_processed(
                wamid=payload.wamid,
                operator_phone=payload.operator_phone,
            )
            if not inserted:
                self._clear_provider_trace_context(self._llm_gateway)
                self._clear_provider_trace_context(self._ad_generation_service)
                return ProcessInboundResult(duplicate=True)

            operator = await operator_repo.get_by_phone(payload.operator_phone)
            if operator is None or not operator.active:
                await audit_repo.log(
                    actor="system",
                    action="inbound_unauthorized_operator_skipped",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
                self._clear_provider_trace_context(self._llm_gateway)
                self._clear_provider_trace_context(self._ad_generation_service)
                return ProcessInboundResult(duplicate=False, unauthorized_operator=True)

            now = utcnow()
            session_obj = await session_repo.get_by_operator_phone(payload.operator_phone)
            session_created = session_obj is None

            history: list[dict[str, str]] = []
            current_draft_id: uuid.UUID | None = None
            pending_upload_type: str | None = None
            pending_followup_question: str | None = None
            pending_question_type = PendingQuestionType.NONE
            pending_question_context: dict[str, Any] = {}
            last_user_intent_hint: str | None = None
            if session_obj is not None:
                history = list(session_obj.history)
                current_draft_id = session_obj.current_draft_id
                pending_upload_type = session_obj.pending_upload_type
                pending_followup_question = session_obj.pending_followup_question
                pending_question_type = session_obj.pending_question_type
                pending_question_context = dict(session_obj.pending_question_context or {})
                last_user_intent_hint = session_obj.last_user_intent_hint

            # --- ONBOARDING GATE ---
            if _operator_needs_onboarding(operator):
                ob_text = _extract_text(payload.raw_message)
                ob_image_id = _extract_image_media_id(payload.raw_message)

                reply_text = await self._handle_onboarding(
                    payload=payload,
                    operator=operator,
                    operator_repo=operator_repo,
                    session_repo=session_repo,
                    audit_repo=audit_repo,
                    session_obj=session_obj,
                    incoming_text=ob_text,
                    incoming_image_media_id=ob_image_id,
                )

                user_text = ob_text or ("[image]" if ob_image_id else "[unsupported]")
                history.append({"role": "user", "text": user_text, "wamid": payload.wamid})
                history.append({"role": "assistant", "text": reply_text, "wamid": payload.wamid})

                await session_repo.create_or_update(
                    operator_phone=payload.operator_phone,
                    language=operator.language,
                    history=history,
                    last_user_intent_hint="onboarding",
                    last_active_at=now,
                )

                # Persist onboarding step if still needed.
                if _operator_needs_onboarding(operator):
                    await session_repo.set_pending_question(
                        operator_phone=payload.operator_phone,
                        pending_question_type=PendingQuestionType.ONBOARDING,
                        pending_question_context={
                            "step": _resolve_onboarding_step(operator),
                        },
                    )

                if session_created:
                    await audit_repo.log(
                        actor="system",
                        action="conversation_session_created",
                        operator_phone=payload.operator_phone,
                        metadata={"wamid": payload.wamid},
                    )
                await audit_repo.log(
                    actor="system",
                    action="inbound_message_processed",
                    operator_phone=payload.operator_phone,
                    metadata={
                        "wamid": payload.wamid,
                        "session_created": session_created,
                        "draft_created": False,
                        "intent": "onboarding",
                        "deterministic_action": "onboarding",
                        "llm_used": False,
                    },
                )
                self._clear_provider_trace_context(self._llm_gateway)
                self._clear_provider_trace_context(self._ad_generation_service)
                return ProcessInboundResult(
                    duplicate=False,
                    session_created=session_created,
                    draft_created=False,
                    llm_used=False,
                    intent="onboarding",
                    deterministic_action="onboarding",
                    reply_text=reply_text,
                )
            # --- END ONBOARDING GATE ---

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
                    currency=operator.currency,
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
            session_language_override: str | None = None
            followup_regen_requested = False

            if button_payload_id:
                history.append(
                    {
                        "role": "user",
                        "text": f"[button:{button_payload_id}]",
                        "wamid": payload.wamid,
                    }
                )
                if pending_question_type == PendingQuestionType.CLASSIFICATION:
                    reply_text = _classification_prompt(operator.language)
                    deterministic_action = "classification_reprompt"
                    intent_value = last_user_intent_hint
                elif button_payload_id == BUTTON_CONFIRM_PUBLISH:
                    deterministic_action = "confirm_publish"
                    intent_value = deterministic_action
                    current_draft, reply_text = await self._confirm_publish_to_cms(
                        payload=payload,
                        draft_repo=draft_repo,
                        published_repo=published_repo,
                        audit_repo=audit_repo,
                        operator=operator,
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
                if pending_question_type == PendingQuestionType.CLASSIFICATION:
                    reply_text = _classification_prompt(operator.language)
                    deterministic_action = "classification_reprompt"
                    intent_value = last_user_intent_hint
                elif pending_upload_type == _PENDING_UPLOAD_LOGO:
                    deterministic_action = "operator_logo_upload"
                    intent_value = deterministic_action
                    reply_text = await self._process_logo_upload(
                        payload=payload,
                        operator_repo=operator_repo,
                        audit_repo=audit_repo,
                        language=operator.language,
                        media_id=incoming_image_media_id,
                    )
                else:
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
                # Upload intent is one-shot; consume it after the first image.
                if pending_question_type != PendingQuestionType.CLASSIFICATION:
                    pending_upload_type = None
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
                    classification: IntentClassification | None = None
                    clear_fields = _parse_operator_clear_request(sanitized_text)
                    if clear_fields:
                        clear_updates = {field_name: None for field_name in clear_fields}
                        await operator_repo.update_branding(
                            payload.operator_phone,
                            **clear_updates,
                        )
                        for field_name in clear_fields:
                            setattr(operator, field_name, None)
                        pending_followup_question = None
                        deterministic_action = "clear_branding_fields"
                        intent_value = deterministic_action
                        await audit_repo.log(
                            actor="system",
                            action="operator_branding_cleared",
                            operator_phone=payload.operator_phone,
                            metadata={
                                "wamid": payload.wamid,
                                "cleared_fields": clear_fields,
                            },
                        )
                        reply_text = _branding_cleared_reply(
                            language=operator.language,
                            cleared_fields=clear_fields,
                        )
                        classification = IntentClassification(intent=Intent.SET_BRANDING)

                    forced_intent: Intent | None = None
                    if (
                        reply_text is None
                        and pending_question_type == PendingQuestionType.CLASSIFICATION
                    ):
                        try:
                            forced_intent = (
                                Intent(last_user_intent_hint)
                                if last_user_intent_hint is not None
                                else Intent.CREATE_AD
                            )
                        except ValueError:
                            forced_intent = Intent.CREATE_AD
                    regenerate_confirmation_unresolved = False
                    if pending_followup_question == _PENDING_FOLLOWUP_REGENERATE_CONFIRMATION:
                        yes_no = _parse_yes_no_answer(
                            message_text=sanitized_text,
                            language=operator.language,
                        )
                        if yes_no is False:
                            pending_followup_question = None
                            reply_text = _regenerate_again_declined_reply(operator.language)
                            if current_draft.rendered_image_url is not None:
                                publish_buttons_prompt = _publish_buttons_prompt(operator.language)
                        elif yes_no is None:
                            regenerate_confirmation_unresolved = True
                        else:
                            followup_regen_requested = True
                            pending_followup_question = None
                            forced_intent = Intent.REGENERATE_WITH_REFERENCE

                    if reply_text is None and pending_followup_question in _PENDING_FOLLOWUP_KEYS:
                        classification = await self._llm_gateway.classify_intent(
                            message_text=sanitized_text,
                            language=operator.language,
                            history=history,
                        )
                        llm_used = self._llm_gateway.uses_external_llm or llm_used
                        if _should_interrupt_pending_followup(classification.intent):
                            pending_followup_question = None
                        else:
                            (
                                current_draft,
                                pending_followup_question,
                                reply_text,
                                llm_used_in_followup,
                            ) = await self._handle_pending_followup_answer(
                                payload=payload,
                                draft_repo=draft_repo,
                                operator_repo=operator_repo,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                operator=operator,
                                history=history,
                                pending_followup_question=pending_followup_question,
                                message_text=sanitized_text,
                            )
                            llm_used = llm_used or llm_used_in_followup

                    if forced_intent is not None:
                        classification = IntentClassification(intent=forced_intent)
                    elif classification is None and reply_text is None:
                        classification = await self._llm_gateway.classify_intent(
                            message_text=sanitized_text,
                            language=operator.language,
                            history=history,
                        )
                        llm_used = self._llm_gateway.uses_external_llm or llm_used
                    elif classification is None:
                        classification = IntentClassification(intent=Intent.UNKNOWN)

                    if (
                        pending_followup_question == _PENDING_FOLLOWUP_REGENERATE_CONFIRMATION
                        and forced_intent is None
                    ):
                        if _should_interrupt_pending_followup(classification.intent):
                            pending_followup_question = None
                        elif regenerate_confirmation_unresolved and reply_text is None:
                            reply_text = _regenerate_again_prompt(operator.language)

                    intent_value = classification.intent.value

                    extracted_fields = None
                    enrichment_notice: str | None = None
                    brand_conflict_followup: str | None = None
                    detected_ean = extract_ean_from_text(sanitized_text)
                    if classification.intent in _AD_INTENTS:
                        if (
                            classification.intent == Intent.CREATE_AD
                            and pending_question_type != PendingQuestionType.CLASSIFICATION
                        ):
                            # For a new ad request, create a fresh draft so
                            # previous ad edits cannot leak into this flow.
                            if not draft_created:
                                previous_draft_id = current_draft.id
                                created_draft = await draft_repo.create(
                                    operator_phone=payload.operator_phone,
                                    status=AdDraftStatus.DRAFT,
                                    currency=operator.currency,
                                )
                                current_draft = created_draft
                                draft_created = True
                                await audit_repo.log(
                                    actor="system",
                                    action="draft_created_for_new_ad",
                                    operator_phone=payload.operator_phone,
                                    metadata={
                                        "wamid": payload.wamid,
                                        "previous_draft_id": str(previous_draft_id),
                                        "new_draft_id": str(current_draft.id),
                                    },
                                )
                            else:
                                # Brand-new session already has an empty draft.
                                refreshed = await draft_repo.reset_product_fields(
                                    draft_id=current_draft.id,
                                    operator_phone=payload.operator_phone,
                                    expected_version=current_draft.version,
                                    currency=operator.currency,
                                )
                                if refreshed is not None:
                                    current_draft = refreshed
                                await audit_repo.log(
                                    actor="system",
                                    action="draft_reused_for_new_ad",
                                    operator_phone=payload.operator_phone,
                                    metadata={
                                        "wamid": payload.wamid,
                                        "draft_id": str(current_draft.id),
                                    },
                                )

                        request_type_decision = await self._decide_request_type(
                            current_draft=current_draft,
                            message_text=sanitized_text,
                            language=operator.language,
                            history=history,
                            pending_classification=(
                                pending_question_type == PendingQuestionType.CLASSIFICATION
                            ),
                        )
                        llm_used = self._llm_gateway.uses_external_llm or llm_used
                        current_draft = await self._persist_request_type_state(
                            payload=payload,
                            draft_repo=draft_repo,
                            session_repo=session_repo,
                            audit_repo=audit_repo,
                            current_draft=current_draft,
                            request_type=request_type_decision.request_type,
                            resolved=request_type_decision.resolved,
                            last_active_at=now,
                            source_intent=classification.intent.value,
                        )
                        if not request_type_decision.resolved:
                            pending_question_type = PendingQuestionType.CLASSIFICATION
                            pending_question_context = {
                                "draft_id": str(current_draft.id),
                                "allowed_request_types": [
                                    AdRequestType.SINGLE_PRODUCT.value,
                                    AdRequestType.MULTI_PRODUCT.value,
                                    AdRequestType.STORE_GENERAL.value,
                                ],
                            }
                            reply_text = _classification_prompt(operator.language)
                        else:
                            pending_question_type = PendingQuestionType.NONE
                            pending_question_context = {}
                            extracted_fields = request_type_decision.extracted_fields

                        if (
                            reply_text is None
                            and request_type_decision.request_type != AdRequestType.SINGLE_PRODUCT
                        ):
                            reply_text = _request_type_resolved_reply(
                                request_type=request_type_decision.request_type,
                                language=operator.language,
                            )
                        if reply_text is None:
                            if extracted_fields is None:
                                extracted_fields = await self._llm_gateway.extract_ad_fields(
                                    message_text=sanitized_text,
                                    language=operator.language,
                                    history=history,
                                )
                                llm_used = self._llm_gateway.uses_external_llm or llm_used
                            update_fields = extracted_fields.to_draft_update_fields()
                            if (
                                "currency" not in update_fields
                                and current_draft.currency != operator.currency
                            ):
                                update_fields["currency"] = operator.currency
                            if update_fields:
                                updated_draft = await draft_repo.update_for_operator_with_version(
                                    draft_id=current_draft.id,
                                    operator_phone=payload.operator_phone,
                                    expected_version=current_draft.version,
                                    **update_fields,
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
                            if followup_regen_requested:
                                branding = await self._llm_gateway.extract_branding_fields(
                                    message_text=sanitized_text,
                                    language=operator.language,
                                )
                                llm_used = self._llm_gateway.uses_external_llm or llm_used
                                branding_update_fields = branding.to_update_kwargs()
                                if branding_update_fields:
                                    await operator_repo.update_branding(
                                        payload.operator_phone,
                                        **branding_update_fields,
                                    )
                                    if "language" in branding_update_fields:
                                        operator.language = str(branding_update_fields["language"])
                                        session_language_override = operator.language
                                    if "store_type" in branding_update_fields:
                                        operator.store_type = branding_update_fields["store_type"]
                                    if "creative_guidance" in branding_update_fields:
                                        operator.creative_guidance = branding_update_fields[
                                            "creative_guidance"
                                        ]
                                    if "business_name" in branding_update_fields:
                                        operator.business_name = branding_update_fields[
                                            "business_name"
                                        ]
                                    if "brand_colors" in branding_update_fields:
                                        operator.brand_colors = branding_update_fields[
                                            "brand_colors"
                                        ]
                                    await audit_repo.log(
                                        actor="system",
                                        action="operator_branding_updated",
                                        operator_phone=payload.operator_phone,
                                        metadata={
                                            "wamid": payload.wamid,
                                            "updated_fields": sorted(branding_update_fields.keys()),
                                            "source": "followup_regenerate_confirmation",
                                        },
                                    )
                            current_draft, enrichment_notice = await self._enrich_current_draft(
                                payload=payload,
                                draft_repo=draft_repo,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                language=operator.language,
                                detected_ean=detected_ean,
                                allow_existing_draft_ean=True,
                            )
                            brand_conflict_followup = _build_brand_conflict_followup(
                                draft=current_draft,
                                language=operator.language,
                            )
                            if brand_conflict_followup is not None:
                                await audit_repo.log(
                                    actor="system",
                                    action="product_brand_conflict_detected",
                                    operator_phone=payload.operator_phone,
                                    metadata={
                                        "wamid": payload.wamid,
                                        "draft_id": str(current_draft.id),
                                        "product_brand": current_draft.product_brand,
                                        "enriched_brand": current_draft.enriched_brand,
                                    },
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
                            await self._send_generation_in_progress(
                                to_phone=payload.operator_phone,
                                language=operator.language,
                            )
                            try:
                                submission = await self._ad_generation_service.submit_for_draft(
                                    draft=_to_generation_draft_input(
                                        draft=current_draft,
                                        operator=operator,
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
                                    # Commit now so the DB lock is released
                                    # while we wait for the generation service.
                                    # expire_on_commit=False keeps loaded
                                    # objects usable after commit.
                                    await session.commit()
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
                                            next_followup = _next_followup_question(
                                                draft=current_draft,
                                                operator=operator,
                                            )
                                            if next_followup is not None:
                                                pending_followup_question = next_followup
                                                followup_prompt = _followup_question_prompt(
                                                    next_followup,
                                                    operator.language,
                                                )
                                                reply_text = f"{reply_text}\n\n{followup_prompt}"
                                            elif not followup_regen_requested:
                                                pending_followup_question = (
                                                    _PENDING_FOLLOWUP_REGENERATE_CONFIRMATION
                                                )
                                                reply_text = (
                                                    f"{reply_text}\n\n"
                                                    f"{_regenerate_again_prompt(operator.language)}"
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
                        if classification.intent in {
                            Intent.CREATE_AD,
                            Intent.REGENERATE_WITH_REFERENCE,
                            Intent.REGENERATE_FROM_SCRATCH,
                        } and not _is_ready_for_generation(current_draft):
                            reply_text = _missing_product_name_reply(operator.language)
                        elif classification.intent == Intent.SET_BRANDING:
                            branding = await self._llm_gateway.extract_branding_fields(
                                message_text=sanitized_text,
                                language=operator.language,
                            )
                            llm_used = self._llm_gateway.uses_external_llm or llm_used
                            update_kwargs = branding.to_update_kwargs()
                            if update_kwargs:
                                await operator_repo.update_branding(
                                    payload.operator_phone, **update_kwargs
                                )
                                if "language" in update_kwargs:
                                    session_language_override = str(update_kwargs["language"])
                                    operator.language = session_language_override
                                if "store_type" in update_kwargs:
                                    operator.store_type = update_kwargs["store_type"]
                                if "creative_guidance" in update_kwargs:
                                    operator.creative_guidance = update_kwargs["creative_guidance"]
                                await audit_repo.log(
                                    actor="system",
                                    action="operator_branding_updated",
                                    operator_phone=payload.operator_phone,
                                    metadata={
                                        "wamid": payload.wamid,
                                        "updated_fields": sorted(update_kwargs.keys()),
                                    },
                                )
                                reply_text = _branding_updated_reply(operator.language)
                            else:
                                reply_text = _branding_not_detected_reply(operator.language)
                        elif classification.intent == Intent.SET_LOGO:
                            pending_upload_type = _PENDING_UPLOAD_LOGO
                            reply_text = _logo_upload_prompt(operator.language)
                        elif classification.intent == Intent.PUBLISH_AD:
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
                    if brand_conflict_followup:
                        if reply_text:
                            reply_text = f"{reply_text}\n\n{brand_conflict_followup}"
                        else:
                            reply_text = brand_conflict_followup
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
                language=(
                    session_language_override
                    if session_language_override is not None
                    else (operator.language if session_created else None)
                ),
                history=history,
                pending_question_type=pending_question_type,
                pending_question_context=pending_question_context,
                last_user_intent_hint=intent_value,
                current_draft_id=current_draft.id,
                pending_upload_type=pending_upload_type,
                pending_followup_question=pending_followup_question,
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
                    metadata={"wamid": payload.wamid, "draft_id": str(current_draft.id)},
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
            self._clear_provider_trace_context(self._llm_gateway)
            self._clear_provider_trace_context(self._ad_generation_service)
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

    def _build_trace_sink(self, *, audit_repo: AuditEventRepository) -> TraceSink:
        async def _trace_sink(action: str, operator_phone: str | None, metadata: dict[str, Any]):
            await audit_repo.log(
                actor="system",
                action=action,
                operator_phone=operator_phone,
                metadata=metadata,
            )

        return _trace_sink

    def _set_provider_trace_context(
        self,
        provider: object,
        *,
        operator_phone: str,
        wamid: str | None,
        trace_sink: TraceSink | None,
    ) -> None:
        setter = getattr(provider, "set_trace_context", None)
        if callable(setter):
            setter(
                operator_phone=operator_phone,
                wamid=wamid,
                trace_sink=trace_sink,
            )

    def _clear_provider_trace_context(self, provider: object) -> None:
        clearer = getattr(provider, "clear_trace_context", None)
        if callable(clearer):
            clearer()

    async def _decide_request_type(
        self,
        *,
        current_draft: AdDraft,
        message_text: str,
        language: str,
        history: list[dict[str, str]],
        pending_classification: bool,
    ) -> RequestTypeDecision:
        if (
            current_draft.is_classification_resolved
            and current_draft.request_type != AdRequestType.UNSET
        ):
            return RequestTypeDecision(
                request_type=current_draft.request_type,
                resolved=True,
            )

        if _contains_any_keyword(message_text, _REQUEST_TYPE_STORE_GENERAL_KEYWORDS):
            return RequestTypeDecision(
                request_type=AdRequestType.STORE_GENERAL,
                resolved=True,
            )
        if _contains_any_keyword(message_text, _REQUEST_TYPE_MULTI_KEYWORDS):
            return RequestTypeDecision(
                request_type=AdRequestType.MULTI_PRODUCT,
                resolved=True,
            )
        if _contains_any_keyword(message_text, _REQUEST_TYPE_SINGLE_KEYWORDS):
            return RequestTypeDecision(
                request_type=AdRequestType.SINGLE_PRODUCT,
                resolved=True,
            )

        extracted_fields = await self._llm_gateway.extract_ad_fields(
            message_text=message_text,
            language=language,
            history=history,
        )
        detected_ean = extract_ean_from_text(message_text)
        if extracted_fields.product_name is not None or detected_ean is not None:
            return RequestTypeDecision(
                request_type=AdRequestType.SINGLE_PRODUCT,
                resolved=True,
                extracted_fields=extracted_fields,
            )

        if pending_classification:
            return RequestTypeDecision(
                request_type=AdRequestType.UNSET,
                resolved=False,
                extracted_fields=extracted_fields,
            )

        return RequestTypeDecision(
            request_type=AdRequestType.UNSET,
            resolved=False,
            extracted_fields=extracted_fields,
        )

    async def _persist_request_type_state(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        session_repo: ConversationSessionRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        request_type: AdRequestType,
        resolved: bool,
        last_active_at: Any,
        source_intent: str | None,
    ) -> AdDraft:
        updated_draft = await draft_repo.update_for_operator_with_version(
            draft_id=current_draft.id,
            operator_phone=payload.operator_phone,
            expected_version=current_draft.version,
            request_type=request_type,
            classification_status=(
                ClassificationStatus.RESOLVED if resolved else ClassificationStatus.PENDING
            ),
            is_classification_resolved=resolved,
        )
        if updated_draft is not None:
            current_draft = updated_draft

        if resolved:
            await session_repo.clear_pending_question(
                operator_phone=payload.operator_phone,
                last_active_at=last_active_at,
            )
            await session_repo.create_or_update(
                operator_phone=payload.operator_phone,
                last_user_intent_hint=source_intent,
            )
            await audit_repo.log(
                actor="system",
                action="request_type_classification_resolved",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "request_type": request_type.value,
                },
            )
            return current_draft

        await session_repo.set_pending_question(
            operator_phone=payload.operator_phone,
            pending_question_type=PendingQuestionType.CLASSIFICATION,
            pending_question_context={
                "draft_id": str(current_draft.id),
                "allowed_request_types": [
                    AdRequestType.SINGLE_PRODUCT.value,
                    AdRequestType.MULTI_PRODUCT.value,
                    AdRequestType.STORE_GENERAL.value,
                ],
            },
            last_active_at=last_active_at,
        )
        await session_repo.create_or_update(
            operator_phone=payload.operator_phone,
            last_user_intent_hint=source_intent,
        )
        await audit_repo.log(
            actor="system",
            action="request_type_classification_ambiguous",
            operator_phone=payload.operator_phone,
            metadata={
                "wamid": payload.wamid,
                "draft_id": str(current_draft.id),
            },
        )
        return current_draft

    async def _confirm_publish_to_cms(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        published_repo: PublishedAdRepository,
        audit_repo: AuditEventRepository,
        operator: Operator,
        current_draft: AdDraft,
        language: str,
    ) -> tuple[AdDraft, str]:
        if current_draft.rendered_image_url is None:
            if language.lower() == "he":
                return current_draft, "אין כרגע תמונת תצוגה מוכנה לפרסום."
            return current_draft, "There is no generated preview image ready for publishing."

        campaign_id = _coerce_positive_int(operator.cms_campaign_id)
        playlist_id = _coerce_positive_int(operator.cms_playlist_id)
        if campaign_id is None or playlist_id is None:
            await audit_repo.log(
                actor="system",
                action="publish_blocked_operator_not_connected",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "cms_campaign_id": operator.cms_campaign_id,
                    "cms_playlist_id": operator.cms_playlist_id,
                },
            )
            return current_draft, _cms_not_connected_reply()

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
                campaign_id=campaign_id,
                playlist_id=playlist_id,
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
            if updated_draft is not None:
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
                    "campaign_id": campaign_id,
                    "playlist_id": playlist_id,
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
                f"[CMS] publish failed draft_id={current_draft.id} error={exc}",
                flush=True,
            )
            if language.lower() == "he":
                return current_draft, f"הפרסום נכשל: {exc}"
            return current_draft, f"Publishing failed: {exc}"

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

    async def _handle_onboarding(
        self,
        *,
        payload: InboundTaskPayload,
        operator: Operator,
        operator_repo: OperatorRepository,
        session_repo: ConversationSessionRepository,
        audit_repo: AuditEventRepository,
        session_obj: ConversationSession | None,
        incoming_text: str | None,
        incoming_image_media_id: str | None,
    ) -> str:
        """Handle the onboarding flow. Returns reply text."""
        language = operator.language
        step = _resolve_onboarding_step(operator)

        # Determine if we've already asked the question for this step.
        already_asked = (
            session_obj is not None
            and session_obj.pending_question_type == PendingQuestionType.ONBOARDING
        )

        if step == _ONBOARDING_STEP_AWAITING_NAME:
            return await self._handle_onboarding_name(
                payload=payload,
                operator=operator,
                operator_repo=operator_repo,
                session_repo=session_repo,
                audit_repo=audit_repo,
                already_asked=already_asked,
                incoming_text=incoming_text,
                incoming_image_media_id=incoming_image_media_id,
                language=language,
            )
        return await self._handle_onboarding_logo(
            payload=payload,
            operator=operator,
            operator_repo=operator_repo,
            session_repo=session_repo,
            audit_repo=audit_repo,
            incoming_image_media_id=incoming_image_media_id,
            language=language,
        )

    async def _handle_onboarding_name(
        self,
        *,
        payload: InboundTaskPayload,
        operator: Operator,
        operator_repo: OperatorRepository,
        session_repo: ConversationSessionRepository,
        audit_repo: AuditEventRepository,
        already_asked: bool,
        incoming_text: str | None,
        incoming_image_media_id: str | None,
        language: str,
    ) -> str:
        """Onboarding step: collect business name from text."""
        # First contact: show welcome and ask for name.
        if not already_asked:
            return _onboarding_welcome_reply(language)

        # Already asked — now process the response.
        if incoming_text is None:
            if incoming_image_media_id is not None:
                return _onboarding_name_expected_text_reply(language)
            return _onboarding_welcome_reply(language)

        business_name = _truncate(incoming_text, 200)
        if business_name is None:
            return _onboarding_welcome_reply(language)

        await operator_repo.update_branding(
            payload.operator_phone,
            business_name=business_name,
        )
        operator.business_name = business_name

        await audit_repo.log(
            actor="system",
            action="onboarding_business_name_captured",
            operator_phone=payload.operator_phone,
            metadata={"wamid": payload.wamid, "business_name": business_name},
        )

        await session_repo.set_pending_question(
            operator_phone=payload.operator_phone,
            pending_question_type=PendingQuestionType.ONBOARDING,
            pending_question_context={"step": _ONBOARDING_STEP_AWAITING_LOGO},
        )

        return _onboarding_name_saved_ask_logo_reply(language, business_name)

    async def _handle_onboarding_logo(
        self,
        *,
        payload: InboundTaskPayload,
        operator: Operator,
        operator_repo: OperatorRepository,
        session_repo: ConversationSessionRepository,
        audit_repo: AuditEventRepository,
        incoming_image_media_id: str | None,
        language: str,
    ) -> str:
        """Onboarding step: collect logo image."""
        if incoming_image_media_id is None:
            return _onboarding_logo_expected_image_reply(language)

        try:
            ingested_photo = await self._operator_photo_ingestor.ingest_whatsapp_image(
                media_id=incoming_image_media_id,
            )
        except MediaIngestError as exc:
            await audit_repo.log(
                actor="system",
                action="onboarding_logo_upload_failed",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "media_id": incoming_image_media_id,
                    "error": str(exc),
                },
            )
            return _onboarding_logo_upload_failed_reply(language)

        await operator_repo.update_branding(
            payload.operator_phone,
            logo_url=ingested_photo.public_url,
        )
        operator.logo_url = ingested_photo.public_url

        await audit_repo.log(
            actor="system",
            action="onboarding_logo_captured",
            operator_phone=payload.operator_phone,
            metadata={
                "wamid": payload.wamid,
                "logo_url": ingested_photo.public_url,
            },
        )

        await session_repo.clear_pending_question(
            operator_phone=payload.operator_phone,
        )

        return _onboarding_complete_reply(language)

    async def _process_logo_upload(
        self,
        *,
        payload: InboundTaskPayload,
        operator_repo: OperatorRepository,
        audit_repo: AuditEventRepository,
        language: str,
        media_id: str,
    ) -> str:
        """Ingest the photo and store it as the operator's logo."""
        try:
            ingested_photo = await self._operator_photo_ingestor.ingest_whatsapp_image(
                media_id=media_id
            )
        except MediaIngestError as exc:
            await audit_repo.log(
                actor="system",
                action="operator_logo_upload_failed",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid, "media_id": media_id, "error": str(exc)},
            )
            return _logo_upload_failed_reply(language)

        await operator_repo.update_branding(
            payload.operator_phone, logo_url=ingested_photo.public_url
        )
        await audit_repo.log(
            actor="system",
            action="operator_logo_updated",
            operator_phone=payload.operator_phone,
            metadata={
                "wamid": payload.wamid,
                "logo_url": ingested_photo.public_url,
            },
        )
        return _logo_saved_reply(language)

    async def _send_generation_in_progress(self, *, to_phone: str, language: str) -> None:
        """Send 'your ad is being created' notice before generation starts."""
        message = _generation_in_progress_message(language)
        try:
            await self._whatsapp_client.send_text(to_phone=to_phone, message=message)
        except Exception:
            logger.warning(
                "Failed to send generation-in-progress message (operator_phone=%s)",
                to_phone,
                exc_info=True,
            )

    async def _handle_pending_followup_answer(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        operator_repo: OperatorRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        operator: Operator,
        history: list[dict[str, str]],
        pending_followup_question: str,
        message_text: str,
    ) -> tuple[AdDraft, str | None, str, bool]:
        llm_used = False

        if pending_followup_question == _PENDING_FOLLOWUP_PRICE:
            extracted_fields = await self._llm_gateway.extract_ad_fields(
                message_text=message_text,
                language=operator.language,
                history=history,
            )
            llm_used = self._llm_gateway.uses_external_llm
            update_fields = extracted_fields.to_draft_update_fields()
            if "price" not in update_fields:
                return (
                    current_draft,
                    pending_followup_question,
                    _followup_reprompt(pending_followup_question, operator.language),
                    llm_used,
                )

            price_update_fields: dict[str, Any] = {"price": update_fields["price"]}
            if "currency" in update_fields:
                price_update_fields["currency"] = update_fields["currency"]
            updated_draft = await draft_repo.update_for_operator_with_version(
                draft_id=current_draft.id,
                operator_phone=payload.operator_phone,
                expected_version=current_draft.version,
                **price_update_fields,
            )
            if updated_draft is None:
                await audit_repo.log(
                    actor="system",
                    action="draft_stale_write_detected",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
                return (
                    current_draft,
                    pending_followup_question,
                    "This draft was already changed. Please refresh and try again.",
                    llm_used,
                )
            current_draft = updated_draft
            await audit_repo.log(
                actor="system",
                action="followup_price_captured",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "price": str(current_draft.price),
                    "currency": current_draft.currency,
                },
            )
        elif pending_followup_question == _PENDING_FOLLOWUP_STORE_TYPE:
            store_type = _truncate(message_text, 120)
            if store_type is None:
                return (
                    current_draft,
                    pending_followup_question,
                    _followup_reprompt(pending_followup_question, operator.language),
                    llm_used,
                )
            await operator_repo.update_branding(payload.operator_phone, store_type=store_type)
            operator.store_type = store_type
            await audit_repo.log(
                actor="system",
                action="followup_store_type_captured",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid, "store_type": store_type},
            )
        elif pending_followup_question == _PENDING_FOLLOWUP_CREATIVE_GUIDANCE:
            creative_guidance = _truncate(message_text, 500)
            if creative_guidance is None:
                return (
                    current_draft,
                    pending_followup_question,
                    _followup_reprompt(pending_followup_question, operator.language),
                    llm_used,
                )
            await operator_repo.update_branding(
                payload.operator_phone,
                creative_guidance=creative_guidance,
            )
            operator.creative_guidance = creative_guidance
            await audit_repo.log(
                actor="system",
                action="followup_creative_guidance_captured",
                operator_phone=payload.operator_phone,
                metadata={"wamid": payload.wamid},
            )

        next_followup = _next_followup_question(draft=current_draft, operator=operator)
        if next_followup is not None:
            return (
                current_draft,
                next_followup,
                _followup_question_prompt(next_followup, operator.language),
                llm_used,
            )
        return (
            current_draft,
            _PENDING_FOLLOWUP_REGENERATE_CONFIRMATION,
            _regenerate_again_prompt(operator.language),
            llm_used,
        )

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
    return draft.product_name is not None


def _to_generation_draft_input(
    *,
    draft: AdDraft,
    operator: Operator,
) -> GenerationDraftInput:
    return GenerationDraftInput(
        draft_id=draft.id,
        operator_phone=operator.phone,
        language=operator.language,
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
        product_brand=draft.product_brand,
        business_name=operator.business_name,
        logo_url=operator.logo_url,
        brand_colors=operator.brand_colors,
        store_type=operator.store_type,
        creative_guidance=operator.creative_guidance,
    )


def _build_brand_conflict_followup(*, draft: AdDraft, language: str) -> str | None:
    if not _has_brand_conflict(draft):
        return None
    operator_brand = draft.product_brand or ""
    catalog_brand = draft.enriched_brand or ""
    if language.lower() == "he":
        return (
            "יצרתי מודעה ראשונית לפי המותג שכתבת. "
            f'רשמתי מותג מוצר: "{operator_brand}" '
            f'בעוד שבמאגר נמצא: "{catalog_brand}". '
            "אם צריך, תכתוב לי איזה מותג לשמור להמשך."
        )
    return (
        "I generated the first ad using the brand you provided. "
        f'You wrote "{operator_brand}", while barcode enrichment found "{catalog_brand}". '
        "If needed, tell me which brand to keep going forward."
    )


def _has_brand_conflict(draft: AdDraft) -> bool:
    operator_brand = _normalize_brand_value(draft.product_brand)
    enriched_brand = _normalize_brand_value(draft.enriched_brand)
    if operator_brand is None or enriched_brand is None:
        return False
    return operator_brand != enriched_brand


def _normalize_brand_value(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = " ".join(value.split()).strip().casefold()
    if not normalized:
        return None
    return normalized


def _generation_in_progress_message(language: str) -> str:
    if language.lower() == "he":
        return "המודעה שלך בתהליך יצירה, אנא המתן..."
    if language.lower() == "ar":
        return "إعلانك قيد الإنشاء، يرجى الانتظار..."
    if language.lower() == "ru":
        return "Ваше объявление создаётся, пожалуйста подождите..."
    return "Your ad is being created, please wait..."


def _generation_completed_reply(language: str) -> str:
    if language.lower() == "he":
        return "התצוגה המקדימה מוכנה."
    return "Your ad preview is ready."


def _next_followup_question(*, draft: AdDraft, operator: Operator) -> str | None:
    if draft.price is None:
        return _PENDING_FOLLOWUP_PRICE
    if _normalize_brand_value(operator.store_type) is None:
        return _PENDING_FOLLOWUP_STORE_TYPE
    if _normalize_brand_value(operator.creative_guidance) is None:
        return _PENDING_FOLLOWUP_CREATIVE_GUIDANCE
    return None


def _should_interrupt_pending_followup(intent: Intent) -> bool:
    return intent in _FOLLOWUP_INTERRUPT_INTENTS


def _followup_question_prompt(question_key: str, language: str) -> str:
    if question_key == _PENDING_FOLLOWUP_PRICE:
        if language.lower() == "he":
            return "מה המחיר המדויק שתרצה להציג במודעה?"
        return "What exact price should I display in the ad?"
    if question_key == _PENDING_FOLLOWUP_STORE_TYPE:
        if language.lower() == "he":
            return "מה סוג העסק שלך? (למשל סופרמרקט, בגדים, תבלינים)"
        return "What is your store type? (for example grocery, clothing, spices)"
    if question_key == _PENDING_FOLLOWUP_CREATIVE_GUIDANCE:
        if language.lower() == "he":
            return "יש לך הנחיות כלליות לסגנון המודעות? (צבעים, טון, אווירה)"
        return "Do you have general creative guidance? (colors, tone, style)"
    if question_key == _PENDING_FOLLOWUP_REGENERATE_CONFIRMATION:
        return _regenerate_again_prompt(language)
    return "Please provide the requested information."


def _followup_reprompt(question_key: str, language: str) -> str:
    if question_key == _PENDING_FOLLOWUP_PRICE:
        if language.lower() == "he":
            return "לא הצלחתי לזהות מחיר. מה המחיר המדויק?"
        return "I could not detect a price. What is the exact price?"
    if language.lower() == "he":
        return "לא הצלחתי לזהות תשובה ברורה. " + _followup_question_prompt(question_key, language)
    return "I could not detect a clear answer. " + _followup_question_prompt(question_key, language)


def _regenerate_again_prompt(language: str) -> str:
    if language.lower() == "he":
        return "כל השאלות הושלמו. רוצה שאפעיל עכשיו יצירת מודעה נוספת? (כן/לא)"
    return (
        "All follow-up questions are complete. Do you want me to generate another ad now? (yes/no)"
    )


def _regenerate_again_declined_reply(language: str) -> str:
    if language.lower() == "he":
        return "מעולה. כשתרצה ליצור מודעה נוספת פשוט תכתוב לי."
    return "Great. When you want another ad, just tell me."


def _parse_yes_no_answer(*, message_text: str, language: str) -> bool | None:
    normalized = _normalize_brand_value(message_text)
    if normalized is None:
        return None

    yes_values = {
        "yes",
        "y",
        "כן",
        "בטח",
        "יאללה",
        "sure",
        "ok",
        "okay",
    }
    no_values = {
        "no",
        "n",
        "לא",
        "לא תודה",
        "not now",
        "cancel",
    }

    if normalized in yes_values:
        return True
    if normalized in no_values:
        return False

    if language.lower() == "he":
        if normalized.startswith("כן"):
            return True
        if normalized.startswith("לא"):
            return False
    else:
        if normalized.startswith("yes"):
            return True
        if normalized.startswith("no"):
            return False
    return None


def _parse_operator_clear_request(message_text: str) -> list[str]:
    normalized = _normalize_brand_value(message_text)
    if normalized is None:
        return []

    clear_keywords = {
        "מחק",
        "תמחק",
        "למחוק",
        "נקה",
        "תנקה",
        "clear",
        "remove",
        "delete",
        "reset",
    }
    if not any(keyword in normalized for keyword in clear_keywords):
        return []

    matched: set[str] = set()
    if any(token in normalized for token in {"מיתוג", "branding"}):
        matched.update(
            {"business_name", "logo_url", "brand_colors", "store_type", "creative_guidance"}
        )
    if any(token in normalized for token in {"לוגו", "logo"}):
        matched.add("logo_url")
    if any(token in normalized for token in {"שם העסק", "business name", "business"}):
        matched.add("business_name")
    if any(token in normalized for token in {"צבע", "צבעים", "colors", "brand colors"}):
        matched.add("brand_colors")
    if any(token in normalized for token in {"סוג העסק", "סוג חנות", "store type"}):
        matched.add("store_type")
    if any(
        token in normalized
        for token in {
            "הנחיה",
            "הנחיות",
            "guidance",
            "style",
            "סגנון",
        }
    ):
        matched.add("creative_guidance")
    return sorted(matched)


def _branding_cleared_reply(*, language: str, cleared_fields: list[str]) -> str:
    if language.lower() == "he":
        labels = {
            "business_name": "שם עסק",
            "logo_url": "לוגו",
            "brand_colors": "צבעי מותג",
            "store_type": "סוג עסק",
            "creative_guidance": "הנחיות כלליות",
        }
        names = ", ".join(labels.get(field, field) for field in cleared_fields)
        return f"ניקיתי את השדות הבאים: {names}."

    labels = {
        "business_name": "business name",
        "logo_url": "logo",
        "brand_colors": "brand colors",
        "store_type": "store type",
        "creative_guidance": "creative guidance",
    }
    names = ", ".join(labels.get(field, field) for field in cleared_fields)
    return f"Cleared the following fields: {names}."


def _missing_product_name_reply(language: str) -> str:
    if language.lower() == "he":
        return "כדי לייצר מודעה אני צריך לפחות שם מוצר. כתוב לי את שם המוצר ונמשיך."
    return "To generate an ad I need at least a product name. Please send the product name."


def _publish_confirmation_prompt(language: str) -> str:
    if language.lower() == "he":
        return "בחר האם לפרסם עכשיו ל-CMS או לבטל."
    return "Choose whether to publish to CMS now or cancel."


def _publish_buttons_prompt(language: str) -> str:
    if language.lower() == "he":
        return "לפרסם את המודעה הזו ל-CMS?"
    return "Publish this ad to CMS?"


def _coerce_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = int(stripped)
        except ValueError:
            return None
        return parsed if parsed > 0 else None
    return None


def _cms_not_connected_reply() -> str:
    return "אתה לא מחובר למערכת כרגע, פנה לתמיכה כדי לייצר את החיבור"


def _generation_failed_reply(language: str) -> str:
    if language.lower() == "he":
        return "יש כרגע תקלה זמנית ביצירת המודעה. נסה שוב בעוד רגע."
    return "Temporary generation service issue. Please try again in a moment."


def _branding_updated_reply(language: str) -> str:
    if language.lower() == "he":
        return "פרטי המיתוג עודכנו בהצלחה."
    if language.lower() == "ar":
        return "تم تحديث معلومات العلامة التجارية بنجاح."
    if language.lower() == "ru":
        return "Данные бренда успешно обновлены."
    return "Branding details updated successfully."


def _branding_not_detected_reply(language: str) -> str:
    if language.lower() == "he":
        return (
            "לא הצלחתי לזהות פרטי מיתוג או העדפות כלליות בהודעה שלך. "
            "נסה לשלוח שם עסק, צבעים, סוג חנות או הנחיות כלליות."
        )
    return (
        "I could not detect branding or general preference details in your message. "
        "Try sending your business name, brand colors, store type, or creative guidance."
    )


# --- Onboarding reply messages ---


def _onboarding_welcome_reply(language: str) -> str:
    if language.lower() == "he":
        return (
            "שלום! ברוך הבא למערכת יצירת המודעות.\n"
            "לפני שנתחיל, אני צריך כמה פרטים.\n\n"
            "מה שם העסק שלך?"
        )
    if language.lower() == "ar":
        return (
            "مرحبًا! أهلاً بك في نظام إنشاء الإعلانات.\n"
            "قبل أن نبدأ، أحتاج بعض التفاصيل.\n\n"
            "ما اسم عملك؟"
        )
    if language.lower() == "ru":
        return (
            "Здравствуйте! Добро пожаловать в систему создания рекламы.\n"
            "Прежде чем начать, мне нужно несколько деталей.\n\n"
            "Как называется ваш бизнес?"
        )
    return (
        "Hello! Welcome to the ad creation system.\n"
        "Before we start, I need a few details.\n\n"
        "What is your business name?"
    )


def _onboarding_name_expected_text_reply(language: str) -> str:
    if language.lower() == "he":
        return "אני צריך קודם את שם העסק שלך. שלח לי את השם כהודעת טקסט."
    if language.lower() == "ar":
        return "أحتاج أولاً إلى اسم عملك. أرسل لي الاسم كرسالة نصية."
    if language.lower() == "ru":
        return "Сначала мне нужно название вашего бизнеса. Отправьте его текстовым сообщением."
    return "I need your business name first. Please send it as a text message."


def _onboarding_name_saved_ask_logo_reply(language: str, business_name: str) -> str:
    if language.lower() == "he":
        return f'תודה! שם העסק "{business_name}" נשמר.\n\nעכשיו שלח לי את הלוגו של העסק כתמונה.'
    if language.lower() == "ar":
        return f'شكرًا! تم حفظ اسم العمل "{business_name}".\n\nالآن أرسل لي شعار العمل كصورة.'
    if language.lower() == "ru":
        return (
            f'Спасибо! Название бизнеса "{business_name}" сохранено.\n\n'
            f"Теперь отправьте мне логотип вашего бизнеса как изображение."
        )
    return (
        f'Thanks! Business name "{business_name}" saved.\n\n'
        f"Now send me your business logo as an image."
    )


def _onboarding_logo_expected_image_reply(language: str) -> str:
    if language.lower() == "he":
        return "אני צריך את הלוגו של העסק. שלח לי אותו כתמונה."
    if language.lower() == "ar":
        return "أحتاج إلى شعار العمل. أرسله لي كصورة."
    if language.lower() == "ru":
        return "Мне нужен логотип вашего бизнеса. Отправьте его как изображение."
    return "I need your business logo. Please send it as an image."


def _onboarding_logo_upload_failed_reply(language: str) -> str:
    if language.lower() == "he":
        return "לא הצלחתי לעבד את התמונה. נסה לשלוח את הלוגו שוב."
    if language.lower() == "ar":
        return "لم أتمكن من معالجة الصورة. حاول إرسال الشعار مرة أخرى."
    if language.lower() == "ru":
        return "Не удалось обработать изображение. Попробуйте отправить логотип ещё раз."
    return "I could not process the image. Please try sending the logo again."


def _onboarding_complete_reply(language: str) -> str:
    if language.lower() == "he":
        return (
            "מעולה! ההרשמה הושלמה בהצלחה ✓\n\n"
            "עכשיו אפשר להתחיל ליצור מודעות. "
            "שלח לי את המוצר או השירות שתרצה לפרסם."
        )
    if language.lower() == "ar":
        return (
            "ممتاز! تم إكمال التسجيل بنجاح ✓\n\n"
            "يمكنك الآن البدء في إنشاء الإعلانات. "
            "أرسل لي المنتج أو الخدمة التي تريد الإعلان عنها."
        )
    if language.lower() == "ru":
        return (
            "Отлично! Регистрация успешно завершена ✓\n\n"
            "Теперь можно начать создавать рекламу. "
            "Отправьте мне товар или услугу, которые хотите рекламировать."
        )
    return (
        "Excellent! Onboarding completed successfully ✓\n\n"
        "You can now start creating ads. "
        "Send me the product or service you'd like to advertise."
    )


def _logo_upload_prompt(language: str) -> str:
    if language.lower() == "he":
        return "שלח לי את הלוגו של העסק שלך כתמונה."
    if language.lower() == "ar":
        return "أرسل لي شعار عملك كصورة."
    if language.lower() == "ru":
        return "Отправьте мне логотип вашего бизнеса как изображение."
    return "Send me your business logo as an image."


def _logo_saved_reply(language: str) -> str:
    if language.lower() == "he":
        return "הלוגו נשמר בהצלחה ✓"
    if language.lower() == "ar":
        return "تم حفظ الشعار بنجاح ✓"
    if language.lower() == "ru":
        return "Логотип успешно сохранён ✓"
    return "Logo saved successfully ✓"


def _logo_upload_failed_reply(language: str) -> str:
    if language.lower() == "he":
        return "לא הצלחתי לעבד את התמונה. נסה לשלוח שוב."
    return "I could not process your image. Please try sending it again."


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
