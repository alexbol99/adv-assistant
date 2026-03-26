import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from adv_assistant import creative_brief_planner, question_policy
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
    AdVariantRoundStatus,
    AdVariantStatus,
    ClassificationStatus,
    PendingQuestionType,
)
from adv_assistant.db.models import AdDraft, ConversationSession, Operator
from adv_assistant.db.repositories import (
    AdDraftRepository,
    AdVariantRepository,
    AdVariantRoundRepository,
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
    BUTTON_CONFIRM_PRODUCT_SELECTION,
    BUTTON_CONFIRM_PUBLISH,
    BUTTON_REJECT_PRODUCT_SELECTION,
    BUTTON_SELECT_VARIANT_A,
    BUTTON_SELECT_VARIANT_B,
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
from adv_assistant.product_discovery import (
    DiscoveryStatus,
    NoopProductDiscoveryService,
    ProductDiscoveryService,
)
from adv_assistant.product_resolution_models import ProductResolutionResult
from adv_assistant.product_resolution_service import (
    NoopProductResolutionService,
    ProductResolutionService,
)
from adv_assistant.tasks_queue import InboundTaskPayload
from adv_assistant.whatsapp import NoopWhatsAppClient, WhatsAppClient

logger = logging.getLogger(__name__)

TraceSink = Callable[[str, str | None, dict[str, Any]], Awaitable[None]]

_ONBOARDING_STEP_AWAITING_NAME = "awaiting_name"
_ONBOARDING_STEP_AWAITING_LOGO = "awaiting_logo"

_PENDING_UPLOAD_LOGO = "logo"
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
_EXPLICIT_NEW_AD_INTERRUPT_KEYWORDS = {
    "new ad",
    "create a new ad",
    "start a new ad",
    "מודעה חדשה",
    "מודעה אחרת",
    "צור מודעה חדשה",
    "להתחיל מודעה חדשה",
}
_LIKELY_CREATE_AD_REQUEST_KEYWORDS = {
    "create ad",
    "create an ad",
    "ad for",
    "i want an ad",
    "i need an ad",
    "מודעה ל",
    "מודעה עבור",
    "צור מודעה",
    "תיצור מודעה",
    "אני רוצה מודעה",
    "אני צריך מודעה",
}
_CONFIRMATION_UNSAFE_IMAGE_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "lookaside.instagram.com",
    "l.instagram.com",
}
_CONFIRMATION_SAFE_IMAGE_SUFFIXES = (
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
    ".gif",
)
_CLASSIFICATION_SOURCE_USER_TEXT_KEY = "source_user_text"


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
    variant_image_urls: list[str] | None = None
    publish_buttons_prompt: str | None = None
    action_buttons_prompt: str | None = None
    action_buttons: list[tuple[str, str]] | None = None

    @property
    def status(self) -> str:
        if self.duplicate:
            return "duplicate_skipped"
        if self.unauthorized_operator:
            return "unauthorized_operator"
        return "processed"


@dataclass(slots=True)
class _GenerationExecutionResult:
    draft: AdDraft
    reply_text: str | None
    generated_image_url: str | None
    variant_image_urls: list[str] | None
    pending_question_type: PendingQuestionType
    pending_question_context: dict[str, Any]
    publish_buttons_prompt: str | None
    action_buttons_prompt: str | None
    action_buttons: list[tuple[str, str]] | None
    deterministic_action: str | None


@dataclass(slots=True)
class _CreativeBriefPlannerTurnResult:
    pending_question_type: PendingQuestionType
    pending_question_context: dict[str, Any]
    reply_text: str | None
    brief_instruction_text: str | None
    resume_intent: Intent | None
    forced_reason: str | None
    validation_fallback: bool


@dataclass(slots=True)
class _ProductConfirmationPromptResult:
    draft: AdDraft
    reply_text: str
    generated_image_url: str | None
    action_buttons_prompt: str
    action_buttons: list[tuple[str, str]]
    pending_question_type: PendingQuestionType
    pending_question_context: dict[str, Any]
    deterministic_action: str


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


def _is_explicit_new_ad_interrupt(message_text: str) -> bool:
    return _contains_any_keyword(message_text, _EXPLICIT_NEW_AD_INTERRUPT_KEYWORDS)


def _is_likely_create_ad_request(message_text: str) -> bool:
    return _contains_any_keyword(message_text, _LIKELY_CREATE_AD_REQUEST_KEYWORDS)


def _classification_source_user_text(
    *,
    pending_question_type: PendingQuestionType,
    pending_question_context: dict[str, Any],
    current_message_text: str,
) -> str:
    if pending_question_type != PendingQuestionType.CLASSIFICATION:
        return current_message_text
    source = pending_question_context.get(_CLASSIFICATION_SOURCE_USER_TEXT_KEY)
    if isinstance(source, str):
        normalized = sanitize_user_text(source, max_chars=2000)
        if normalized:
            return normalized
    return current_message_text


def _classification_context_message_text(
    *,
    source_user_text: str,
    current_message_text: str,
) -> str:
    normalized_current = sanitize_user_text(current_message_text, max_chars=2000)
    normalized_source = sanitize_user_text(source_user_text, max_chars=2000)
    if not normalized_source:
        return normalized_current
    if normalized_source == normalized_current:
        return normalized_current
    # Keep both so we preserve the original product request while also
    # retaining the explicit classification follow-up answer.
    return f"{normalized_source}\n{normalized_current}"


def _classification_prompt(language: str) -> str:
    return question_policy.classification_prompt(language)


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


def _mandatory_short_circuit_question(*, question_key: str, language: str) -> str:
    if question_key == question_policy.QUESTION_KEY_PRICE:
        if language.lower() == "he":
            return "מה מחיר המבצע?"
        return "What is the sale price?"
    return question_policy.question_prompt(question_key, language)


class InboundTaskProcessor:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        llm_gateway: LLMGateway | None = None,
        enrichment_service: EnrichmentService | None = None,
        product_discovery_service: ProductDiscoveryService | None = None,
        ad_generation_service: AdGenerationService | None = None,
        render_width: int = 1920,
        render_height: int = 1080,
        operator_photo_ingestor: OperatorPhotoIngestor | None = None,
        cms_publisher: CMSPublisher | None = None,
        whatsapp_client: WhatsAppClient | None = None,
        product_resolution_service: ProductResolutionService | None = None,
        pipeline_v1_enabled: bool = True,
    ) -> None:
        self._session_factory = session_factory
        self._llm_gateway = llm_gateway or NoopLLMGateway()
        self._enrichment_service = enrichment_service or NoopEnrichmentService()
        self._product_discovery_service = product_discovery_service or NoopProductDiscoveryService()
        self._ad_generation_service = ad_generation_service or NoopAdGenerationService()
        self._render_width = render_width
        self._render_height = render_height
        self._operator_photo_ingestor = operator_photo_ingestor or NoopOperatorPhotoIngestor()
        self._cms_publisher = cms_publisher or NoopCMSPublisher()
        self._whatsapp_client = whatsapp_client or NoopWhatsAppClient()
        self._product_resolution_service = (
            product_resolution_service or NoopProductResolutionService()
        )
        self._pipeline_v1_enabled = pipeline_v1_enabled

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
            resolve_after_confirm = self._resolve_generation_instruction_after_product_confirmation

            inserted = await processed_repo.mark_processed(
                wamid=payload.wamid,
                operator_phone=payload.operator_phone,
            )
            if not inserted:
                self._clear_provider_trace_context(self._llm_gateway)
                self._clear_provider_trace_context(self._ad_generation_service)
                return ProcessInboundResult(duplicate=True)
            # Keep the dedupe write transaction short so concurrent inbound messages
            # are less likely to hit SQLite "database is locked" contention.
            await session.commit()

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

            if not self._pipeline_v1_enabled:
                await audit_repo.log(
                    actor="system",
                    action="pipeline_v1_disabled",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
                logger.info(
                    "Pipeline V1 disabled, skipping processing (wamid=%s)",
                    payload.wamid,
                )
                self._clear_provider_trace_context(self._llm_gateway)
                self._clear_provider_trace_context(self._ad_generation_service)
                return ProcessInboundResult(
                    duplicate=False,
                    reply_text="The service is temporarily unavailable. Please try again later.",
                )

            now = utcnow()
            session_obj = await session_repo.get_by_operator_phone(payload.operator_phone)
            session_created = session_obj is None

            history: list[dict[str, str]] = []
            current_draft_id: uuid.UUID | None = None
            pending_upload_type: str | None = None
            pending_question_type = PendingQuestionType.NONE
            pending_question_context: dict[str, Any] = {}
            last_user_intent_hint: str | None = None
            if session_obj is not None:
                history = list(session_obj.history)
                current_draft_id = session_obj.current_draft_id
                pending_upload_type = session_obj.pending_upload_type
                pending_question_type = session_obj.pending_question_type
                pending_question_context = dict(session_obj.pending_question_context or {})
                pending_question_type, pending_question_context = (
                    question_policy.normalize_legacy_pending_followup(
                        pending_question_type=pending_question_type,
                        pending_question_context=pending_question_context,
                        pending_followup_question=session_obj.pending_followup_question,
                    )
                )
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
            variant_image_urls: list[str] | None = None
            publish_buttons_prompt: str | None = None
            action_buttons_prompt: str | None = None
            action_buttons: list[tuple[str, str]] | None = None
            session_language_override: str | None = None
            followup_regen_requested = False
            brief_instruction_override: str | None = None

            if button_payload_id:
                history.append(
                    {
                        "role": "user",
                        "text": f"[button:{button_payload_id}]",
                        "wamid": payload.wamid,
                    }
                )
                if pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION:
                    if button_payload_id == BUTTON_CONFIRM_PRODUCT_SELECTION:
                        intent_value = button_payload_id
                        pending_question_type = PendingQuestionType.NONE
                        pending_question_context = {}
                        updated_draft = await draft_repo.update_for_operator_with_version(
                            draft_id=current_draft.id,
                            operator_phone=payload.operator_phone,
                            expected_version=current_draft.version,
                            awaiting_product_confirmation=False,
                        )
                        if updated_draft is None:
                            deterministic_action = "product_confirmation_stale"
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
                            await audit_repo.log(
                                actor="system",
                                action="product_confirmation_approved",
                                operator_phone=payload.operator_phone,
                                metadata={
                                    "wamid": payload.wamid,
                                    "draft_id": str(current_draft.id),
                                    "photo_url": current_draft.photo_url,
                                },
                            )
                            planner_turn = await self._advance_creative_brief_planner(
                                payload=payload,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                operator=operator,
                                history=history,
                                pending_question_context=pending_question_context,
                                conversation_session=session_obj,
                                source_intent=Intent.CREATE_AD,
                                latest_user_message=None,
                            )
                            llm_used = self._llm_gateway.uses_external_llm or llm_used
                            if planner_turn.reply_text is not None:
                                pending_question_type = planner_turn.pending_question_type
                                pending_question_context = planner_turn.pending_question_context
                                reply_text = planner_turn.reply_text
                                deterministic_action = "generation_gate_blocked"
                            else:
                                (
                                    current_draft,
                                    marketing_instruction_text,
                                    brief_llm_used,
                                ) = await resolve_after_confirm(
                                    payload=payload,
                                    draft_repo=draft_repo,
                                    audit_repo=audit_repo,
                                    current_draft=current_draft,
                                    operator=operator,
                                )
                                llm_used = llm_used or brief_llm_used
                                fallback_instruction = _product_confirmation_generation_instruction(
                                    operator.language
                                )
                                planner_instruction_text = (
                                    planner_turn.brief_instruction_text or fallback_instruction
                                )
                                generation_instruction_text = marketing_instruction_text
                                if (
                                    marketing_instruction_text.strip()
                                    == fallback_instruction.strip()
                                ):
                                    generation_instruction_text = planner_instruction_text
                                generation_result = await self._execute_generation(
                                    session=session,
                                    payload=payload,
                                    draft_repo=draft_repo,
                                    audit_repo=audit_repo,
                                    current_draft=current_draft,
                                    operator=operator,
                                    mode=GenerationMode.FRESH,
                                    instruction_text=generation_instruction_text,
                                    followup_regen_requested=False,
                                )
                                current_draft = generation_result.draft
                                reply_text = generation_result.reply_text
                                generated_image_url = generation_result.generated_image_url
                                variant_image_urls = generation_result.variant_image_urls
                                pending_question_type = generation_result.pending_question_type
                                pending_question_context = (
                                    generation_result.pending_question_context
                                )
                                publish_buttons_prompt = generation_result.publish_buttons_prompt
                                action_buttons_prompt = generation_result.action_buttons_prompt
                                action_buttons = generation_result.action_buttons
                                deterministic_action = (
                                    generation_result.deterministic_action
                                    or "product_confirmation_approved"
                                )
                    elif button_payload_id == BUTTON_REJECT_PRODUCT_SELECTION:
                        intent_value = button_payload_id
                        pending_question_type = PendingQuestionType.NONE
                        pending_question_context = {}
                        updated_draft = await draft_repo.update_for_operator_with_version(
                            draft_id=current_draft.id,
                            operator_phone=payload.operator_phone,
                            expected_version=current_draft.version,
                            awaiting_product_confirmation=False,
                            photo_url=None,
                            enriched_image_url=None,
                        )
                        if updated_draft is None:
                            deterministic_action = "product_confirmation_stale"
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
                            deterministic_action = "product_confirmation_rejected"
                            reply_text = _product_confirmation_rejected_reply(operator.language)
                            await audit_repo.log(
                                actor="system",
                                action="product_confirmation_rejected",
                                operator_phone=payload.operator_phone,
                                metadata={
                                    "wamid": payload.wamid,
                                    "draft_id": str(current_draft.id),
                                },
                            )
                    else:
                        deterministic_action = "product_confirmation_reprompt"
                        intent_value = deterministic_action
                        reply_text = _product_confirmation_use_buttons_reply(operator.language)
                elif pending_question_type == PendingQuestionType.VARIANT_SELECTION:
                    if button_payload_id in (
                        BUTTON_SELECT_VARIANT_A,
                        BUTTON_SELECT_VARIANT_B,
                    ):
                        slot_no = 1 if button_payload_id == BUTTON_SELECT_VARIANT_A else 2
                        slot_label = "A" if slot_no == 1 else "B"
                        intent_value = button_payload_id
                        round_id_str = pending_question_context.get("round_id")
                        selected_variant = None
                        if round_id_str:
                            round_id_val = uuid.UUID(round_id_str)
                            variants = await AdVariantRepository(session).list_by_round_id(
                                round_id_val
                            )
                            selected_variant = next(
                                (v for v in variants if v.slot_no == slot_no),
                                None,
                            )
                        if selected_variant is not None:
                            updated_draft = await draft_repo.update_for_operator_with_version(
                                draft_id=current_draft.id,
                                operator_phone=payload.operator_phone,
                                expected_version=current_draft.version,
                                selected_variant_id=selected_variant.id,
                                selected_round_id=selected_variant.round_id,
                                rendered_image_url=selected_variant.image_url,
                                preview_reference_url=selected_variant.image_url,
                            )
                            if updated_draft is None:
                                deterministic_action = "variant_selection_stale"
                                reply_text = (
                                    "This draft was already changed. Please refresh and try again."
                                )
                            else:
                                current_draft = updated_draft
                                deterministic_action = "variant_selected"
                                base_reply = _variant_selected_reply(operator.language, slot_label)
                                pending_question_type = PendingQuestionType.NONE
                                pending_question_context = {}
                                reply_text = base_reply
                                publish_buttons_prompt = _publish_buttons_prompt(operator.language)
                                await audit_repo.log(
                                    actor="system",
                                    action="variant_selected",
                                    operator_phone=payload.operator_phone,
                                    metadata={
                                        "wamid": payload.wamid,
                                        "draft_id": str(current_draft.id),
                                        "variant_id": str(selected_variant.id),
                                        "slot_no": slot_no,
                                    },
                                )
                        else:
                            deterministic_action = "variant_selection_failed"
                            reply_text = _variant_selection_use_buttons_reply(operator.language)
                    else:
                        deterministic_action = "variant_selection_reprompt"
                        intent_value = deterministic_action
                        reply_text = _variant_selection_use_buttons_reply(operator.language)
                elif pending_question_type == PendingQuestionType.CLASSIFICATION:
                    reply_text = _classification_prompt(operator.language)
                    deterministic_action = "classification_reprompt"
                    intent_value = last_user_intent_hint
                elif button_payload_id == BUTTON_CONFIRM_PUBLISH:
                    deterministic_action = "confirm_publish"
                    intent_value = deterministic_action
                    (
                        current_draft,
                        reply_text,
                        publish_resolved,
                    ) = await self._confirm_publish_to_cms(
                        payload=payload,
                        draft_repo=draft_repo,
                        published_repo=published_repo,
                        audit_repo=audit_repo,
                        operator=operator,
                        current_draft=current_draft,
                        language=operator.language,
                    )
                    if publish_resolved:
                        previous_draft_id = current_draft.id
                        current_draft = await draft_repo.create(
                            operator_phone=payload.operator_phone,
                            status=AdDraftStatus.DRAFT,
                            currency=operator.currency,
                        )
                        draft_created = True
                        pending_question_type = PendingQuestionType.NONE
                        pending_question_context = {}
                        publish_buttons_prompt = None
                        action_buttons_prompt = None
                        action_buttons = None
                        await audit_repo.log(
                            actor="system",
                            action="draft_reset_after_publish_decision",
                            operator_phone=payload.operator_phone,
                            metadata={
                                "wamid": payload.wamid,
                                "decision": "apply",
                                "previous_draft_id": str(previous_draft_id),
                                "new_draft_id": str(current_draft.id),
                            },
                        )
                elif button_payload_id == BUTTON_CANCEL_PUBLISH:
                    deterministic_action = "cancel_publish"
                    intent_value = deterministic_action
                    reply_text = _publish_canceled_reply(operator.language)
                    if current_draft.rendered_image_url is not None or current_draft.status in {
                        AdDraftStatus.PREVIEW_READY,
                        AdDraftStatus.PUBLISHED,
                    }:
                        previous_draft_id = current_draft.id
                        current_draft = await draft_repo.create(
                            operator_phone=payload.operator_phone,
                            status=AdDraftStatus.DRAFT,
                            currency=operator.currency,
                        )
                        draft_created = True
                        pending_question_type = PendingQuestionType.NONE
                        pending_question_context = {}
                        publish_buttons_prompt = None
                        action_buttons_prompt = None
                        action_buttons = None
                        await audit_repo.log(
                            actor="system",
                            action="draft_reset_after_publish_decision",
                            operator_phone=payload.operator_phone,
                            metadata={
                                "wamid": payload.wamid,
                                "decision": "cancel",
                                "previous_draft_id": str(previous_draft_id),
                                "new_draft_id": str(current_draft.id),
                            },
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
                if pending_upload_type == _PENDING_UPLOAD_LOGO:
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
                    can_start_fresh_image_flow = (
                        pending_question_type == PendingQuestionType.NONE
                        and current_draft.status == AdDraftStatus.DRAFT
                        and not current_draft.awaiting_product_confirmation
                        and current_draft.photo_url is None
                        and current_draft.rendered_image_url is None
                        and current_draft.preview_reference_url is None
                    )
                    is_image_first_path = (
                        pending_question_type == PendingQuestionType.CLASSIFICATION
                        or current_draft.request_type == AdRequestType.UNSET
                        or can_start_fresh_image_flow
                    )
                    deterministic_action = "operator_photo_ingest"
                    if is_image_first_path:
                        intent_value = Intent.CREATE_AD.value
                    else:
                        intent_value = deterministic_action
                    previous_pending_question_type = pending_question_type
                    previous_pending_question_context = dict(pending_question_context)
                    previous_photo_url = current_draft.photo_url
                    current_draft, reply_text = await self._process_operator_photo_message(
                        payload=payload,
                        draft_repo=draft_repo,
                        audit_repo=audit_repo,
                        current_draft=current_draft,
                        language=operator.language,
                        media_id=incoming_image_media_id,
                    )
                    photo_ingested = current_draft.photo_url is not None and (
                        previous_photo_url is None or current_draft.photo_url != previous_photo_url
                    )
                    if is_image_first_path and photo_ingested:
                        updated_draft = await draft_repo.update_for_operator_with_version(
                            draft_id=current_draft.id,
                            operator_phone=payload.operator_phone,
                            expected_version=current_draft.version,
                            request_type=AdRequestType.SINGLE_PRODUCT,
                            classification_status=ClassificationStatus.RESOLVED,
                            is_classification_resolved=True,
                            awaiting_product_confirmation=True,
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
                            pending_question_type = previous_pending_question_type
                            pending_question_context = previous_pending_question_context
                        else:
                            current_draft = updated_draft
                            confirmation_prompt = (
                                await self._build_product_confirmation_requested_response(
                                    payload=payload,
                                    draft_repo=draft_repo,
                                    audit_repo=audit_repo,
                                    current_draft=current_draft,
                                    language=operator.language,
                                )
                            )
                            current_draft = confirmation_prompt.draft
                            deterministic_action = confirmation_prompt.deterministic_action
                            generated_image_url = confirmation_prompt.generated_image_url
                            reply_text = confirmation_prompt.reply_text
                            action_buttons_prompt = confirmation_prompt.action_buttons_prompt
                            action_buttons = confirmation_prompt.action_buttons
                            pending_question_type = confirmation_prompt.pending_question_type
                            pending_question_context = confirmation_prompt.pending_question_context
                    elif is_image_first_path:
                        pending_question_type = previous_pending_question_type
                        pending_question_context = previous_pending_question_context
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
                    if pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION:
                        yes_no = _parse_yes_no_answer(
                            message_text=sanitized_text,
                            language=operator.language,
                        )
                        if yes_no is None:
                            reply_text = _product_confirmation_use_buttons_reply(operator.language)
                            deterministic_action = "product_confirmation_reprompt"
                        elif yes_no is False:
                            pending_question_type = PendingQuestionType.NONE
                            pending_question_context = {}
                            updated_draft = await draft_repo.update_for_operator_with_version(
                                draft_id=current_draft.id,
                                operator_phone=payload.operator_phone,
                                expected_version=current_draft.version,
                                awaiting_product_confirmation=False,
                                photo_url=None,
                                enriched_image_url=None,
                            )
                            if updated_draft is None:
                                deterministic_action = "product_confirmation_stale"
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
                                deterministic_action = "product_confirmation_rejected"
                                reply_text = _product_confirmation_rejected_reply(operator.language)
                                await audit_repo.log(
                                    actor="system",
                                    action="product_confirmation_rejected",
                                    operator_phone=payload.operator_phone,
                                    metadata={
                                        "wamid": payload.wamid,
                                        "draft_id": str(current_draft.id),
                                    },
                                )
                        else:
                            pending_question_type = PendingQuestionType.NONE
                            pending_question_context = {}
                            updated_draft = await draft_repo.update_for_operator_with_version(
                                draft_id=current_draft.id,
                                operator_phone=payload.operator_phone,
                                expected_version=current_draft.version,
                                awaiting_product_confirmation=False,
                            )
                            if updated_draft is None:
                                deterministic_action = "product_confirmation_stale"
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
                                await audit_repo.log(
                                    actor="system",
                                    action="product_confirmation_approved",
                                    operator_phone=payload.operator_phone,
                                    metadata={
                                        "wamid": payload.wamid,
                                        "draft_id": str(current_draft.id),
                                        "photo_url": current_draft.photo_url,
                                    },
                                )
                                planner_turn = await self._advance_creative_brief_planner(
                                    payload=payload,
                                    audit_repo=audit_repo,
                                    current_draft=current_draft,
                                    operator=operator,
                                    history=history,
                                    pending_question_context=pending_question_context,
                                    conversation_session=session_obj,
                                    source_intent=Intent.CREATE_AD,
                                    latest_user_message=None,
                                )
                                llm_used = self._llm_gateway.uses_external_llm or llm_used
                                if planner_turn.reply_text is not None:
                                    pending_question_type = planner_turn.pending_question_type
                                    pending_question_context = planner_turn.pending_question_context
                                    reply_text = planner_turn.reply_text
                                    deterministic_action = "generation_gate_blocked"
                                else:
                                    (
                                        current_draft,
                                        marketing_instruction_text,
                                        brief_llm_used,
                                    ) = await resolve_after_confirm(
                                        payload=payload,
                                        draft_repo=draft_repo,
                                        audit_repo=audit_repo,
                                        current_draft=current_draft,
                                        operator=operator,
                                    )
                                    llm_used = llm_used or brief_llm_used
                                    fallback_instruction = (
                                        _product_confirmation_generation_instruction(
                                            operator.language
                                        )
                                    )
                                    planner_instruction_text = (
                                        planner_turn.brief_instruction_text or fallback_instruction
                                    )
                                    generation_instruction_text = marketing_instruction_text
                                    if (
                                        marketing_instruction_text.strip()
                                        == fallback_instruction.strip()
                                    ):
                                        generation_instruction_text = planner_instruction_text
                                    generation_result = await self._execute_generation(
                                        session=session,
                                        payload=payload,
                                        draft_repo=draft_repo,
                                        audit_repo=audit_repo,
                                        current_draft=current_draft,
                                        operator=operator,
                                        mode=GenerationMode.FRESH,
                                        instruction_text=generation_instruction_text,
                                        followup_regen_requested=False,
                                    )
                                    current_draft = generation_result.draft
                                    reply_text = generation_result.reply_text
                                    generated_image_url = generation_result.generated_image_url
                                    variant_image_urls = generation_result.variant_image_urls
                                    pending_question_type = generation_result.pending_question_type
                                    pending_question_context = (
                                        generation_result.pending_question_context
                                    )
                                    publish_buttons_prompt = (
                                        generation_result.publish_buttons_prompt
                                    )
                                    action_buttons_prompt = generation_result.action_buttons_prompt
                                    action_buttons = generation_result.action_buttons
                                    deterministic_action = (
                                        generation_result.deterministic_action
                                        or "product_confirmation_approved"
                                    )
                        classification = IntentClassification(intent=Intent.UNKNOWN)
                    clear_fields = _parse_operator_clear_request(sanitized_text)
                    if clear_fields:
                        clear_updates = {field_name: None for field_name in clear_fields}
                        await operator_repo.update_branding(
                            payload.operator_phone,
                            **clear_updates,
                        )
                        for field_name in clear_fields:
                            setattr(operator, field_name, None)
                        if pending_question_type in {
                            PendingQuestionType.MISSING_INFO,
                            PendingQuestionType.GENERATION_RETRY,
                        }:
                            pending_question_type = PendingQuestionType.NONE
                            pending_question_context = {}
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
                    clarification_budget_context_override: dict[str, Any] | None = None
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
                    if reply_text is None and _is_creative_brief_pending(
                        pending_question_type=pending_question_type,
                        pending_question_context=pending_question_context,
                    ):
                        interrupt_classification = await self._llm_gateway.classify_intent(
                            message_text=sanitized_text,
                            language=operator.language,
                            history=history,
                        )
                        llm_used = self._llm_gateway.uses_external_llm or llm_used
                        if (
                            interrupt_classification.intent
                            in question_policy.INTERRUPT_PENDING_QUESTION_INTENTS
                        ):
                            if (
                                interrupt_classification.intent == Intent.CREATE_AD
                                and not _is_explicit_new_ad_interrupt(sanitized_text)
                            ):
                                interrupt_classification = IntentClassification(
                                    intent=Intent.UNKNOWN
                                )
                            else:
                                pending_question_type = PendingQuestionType.NONE
                                pending_question_context = {}
                                forced_intent = interrupt_classification.intent
                        if forced_intent is None:
                            planner_turn = await self._advance_creative_brief_planner(
                                payload=payload,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                operator=operator,
                                history=history,
                                pending_question_context=pending_question_context,
                                conversation_session=session_obj,
                                source_intent=last_user_intent_hint,
                                latest_user_message=sanitized_text,
                            )
                            llm_used = self._llm_gateway.uses_external_llm or llm_used
                            pending_question_type = planner_turn.pending_question_type
                            pending_question_context = planner_turn.pending_question_context
                            if planner_turn.reply_text is not None:
                                reply_text = planner_turn.reply_text
                                deterministic_action = "generation_gate_blocked"
                                classification = IntentClassification(intent=Intent.UNKNOWN)
                            else:
                                brief_instruction_override = planner_turn.brief_instruction_text
                                resume_intent = planner_turn.resume_intent
                                if resume_intent in {None, Intent.UNKNOWN}:
                                    forced_intent = Intent.REGENERATE_FROM_SCRATCH
                                else:
                                    forced_intent = resume_intent
                    pending_question_key = question_policy.pending_question_key(
                        pending_question_type=pending_question_type,
                        pending_question_context=pending_question_context,
                    )
                    if (
                        reply_text is None
                        and not _is_creative_brief_pending(
                            pending_question_type=pending_question_type,
                            pending_question_context=pending_question_context,
                        )
                        and pending_question_key is not None
                        and pending_question_type
                        in {
                            PendingQuestionType.MISSING_INFO,
                            PendingQuestionType.GENERATION_RETRY,
                        }
                    ):
                        transition_status: question_policy.PendingResolutionStatus | None = None
                        pending_handled = False
                        if pending_question_type == PendingQuestionType.GENERATION_RETRY:
                            yes_no = _parse_yes_no_answer(
                                message_text=sanitized_text,
                                language=operator.language,
                            )
                            if yes_no is True:
                                followup_regen_requested = True
                                forced_intent = Intent.REGENERATE_WITH_REFERENCE
                                transition_status = question_policy.PendingResolutionStatus.RESOLVED
                                pending_handled = True
                            elif yes_no is False:
                                transition_status = question_policy.PendingResolutionStatus.RESOLVED
                                if current_draft.rendered_image_url is not None:
                                    reply_text = _publish_confirmation_prompt(operator.language)
                                    publish_buttons_prompt = _publish_buttons_prompt(
                                        operator.language
                                    )
                                pending_handled = True
                            else:
                                classification = await self._llm_gateway.classify_intent(
                                    message_text=sanitized_text,
                                    language=operator.language,
                                    history=history,
                                )
                                llm_used = self._llm_gateway.uses_external_llm or llm_used
                                if (
                                    classification.intent == Intent.CREATE_AD
                                    and not _is_explicit_new_ad_interrupt(sanitized_text)
                                    and not _is_likely_create_ad_request(sanitized_text)
                                ):
                                    followup_regen_requested = True
                                    forced_intent = Intent.REGENERATE_WITH_REFERENCE
                                    transition_status = (
                                        question_policy.PendingResolutionStatus.RESOLVED
                                    )
                                elif (
                                    classification.intent
                                    in question_policy.INTERRUPT_PENDING_QUESTION_INTENTS
                                ):
                                    transition_status = (
                                        question_policy.PendingResolutionStatus.INTERRUPTED
                                    )
                                else:
                                    transition_status = (
                                        question_policy.PendingResolutionStatus.RESOLVED
                                    )
                                    classification = IntentClassification(intent=Intent.UNKNOWN)
                                    if current_draft.rendered_image_url is not None:
                                        reply_text = _publish_confirmation_prompt(operator.language)
                                        publish_buttons_prompt = _publish_buttons_prompt(
                                            operator.language
                                        )
                                pending_handled = True
                        elif pending_question_type == PendingQuestionType.MISSING_INFO:
                            (
                                current_draft,
                                transition_status,
                                pending_reply_text,
                                llm_used_in_pending,
                            ) = await self._resolve_pending_missing_info_answer(
                                payload=payload,
                                draft_repo=draft_repo,
                                operator_repo=operator_repo,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                operator=operator,
                                history=history,
                                pending_question_context=pending_question_context,
                                message_text=sanitized_text,
                            )
                            llm_used = llm_used or llm_used_in_pending
                            if pending_reply_text is not None:
                                reply_text = pending_reply_text
                            classification = IntentClassification(intent=Intent.UNKNOWN)
                            if (
                                transition_status
                                == question_policy.PendingResolutionStatus.UNRESOLVED
                            ):
                                classification = await self._llm_gateway.classify_intent(
                                    message_text=sanitized_text,
                                    language=operator.language,
                                    history=history,
                                )
                                llm_used = self._llm_gateway.uses_external_llm or llm_used
                                if (
                                    classification.intent
                                    in question_policy.INTERRUPT_PENDING_QUESTION_INTENTS
                                ):
                                    if (
                                        classification.intent == Intent.CREATE_AD
                                        and not _is_explicit_new_ad_interrupt(sanitized_text)
                                    ):
                                        classification = IntentClassification(intent=Intent.UNKNOWN)
                                    else:
                                        transition_status = (
                                            question_policy.PendingResolutionStatus.INTERRUPTED
                                        )
                                else:
                                    classification = IntentClassification(intent=Intent.UNKNOWN)
                            pending_handled = True
                        if pending_handled and transition_status is not None:
                            next_question = None
                            is_pre_generation_phase = False
                            if (
                                transition_status
                                == question_policy.PendingResolutionStatus.RESOLVED
                                and forced_intent is None
                            ):
                                phase = pending_question_context.get(
                                    question_policy.QUESTION_CONTEXT_PHASE
                                )
                                is_post_preview_phase = (
                                    phase == question_policy.QUESTION_PHASE_POST_PREVIEW
                                )
                                is_pre_generation_phase = (
                                    phase == question_policy.QUESTION_PHASE_PRE_GENERATION
                                )
                                if is_post_preview_phase:
                                    # Post-preview followups now end with publish confirmation,
                                    # without another "regenerate again?" question.
                                    next_question = None
                                    forced_intent = Intent.PUBLISH_AD
                                elif is_pre_generation_phase:
                                    next_question = self._select_next_question(
                                        current_draft=current_draft,
                                        operator=operator,
                                        after_preview_generation=False,
                                        allow_regenerate_confirmation=False,
                                        current_pending_question_context=pending_question_context,
                                    )
                                    if next_question is None and reply_text is None:
                                        clarification_budget_context_override = dict(
                                            pending_question_context
                                        )
                            pending_transition = question_policy.resolve_pending_question(
                                pending_question_type=pending_question_type,
                                pending_question_context=pending_question_context,
                                status=transition_status,
                                language=operator.language,
                                next_question=next_question,
                            )
                            pending_question_type = pending_transition.pending_question_type
                            pending_question_context = pending_transition.pending_question_context
                            if reply_text is None and pending_transition.reply_text is not None:
                                reply_text = pending_transition.reply_text
                            if (
                                transition_status
                                == question_policy.PendingResolutionStatus.RESOLVED
                                and forced_intent is None
                                and is_pre_generation_phase
                                and next_question is None
                                and reply_text is None
                            ):
                                # Continue immediately to generation once pre-generation
                                # clarification handling is complete.
                                forced_intent = Intent.REGENERATE_FROM_SCRATCH

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

                    has_selected_preview_context = (
                        pending_question_type == PendingQuestionType.NONE
                        and current_draft.selected_variant_id is not None
                        and current_draft.rendered_image_url is not None
                        and current_draft.status
                        in {AdDraftStatus.PREVIEW_READY, AdDraftStatus.PUBLISHED}
                    )
                    if (
                        classification.intent == Intent.CREATE_AD
                        and (
                            pending_question_type == PendingQuestionType.VARIANT_SELECTION
                            or has_selected_preview_context
                        )
                        and not _is_explicit_new_ad_interrupt(sanitized_text)
                        and not _is_likely_create_ad_request(sanitized_text)
                    ):
                        # Free-text edits after preview generation should stay on the
                        # current draft unless the operator explicitly asks for a new ad.
                        followup_regen_requested = True
                        classification = IntentClassification(
                            intent=Intent.REGENERATE_WITH_REFERENCE
                        )

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

                        classification_source_text = _classification_source_user_text(
                            pending_question_type=pending_question_type,
                            pending_question_context=pending_question_context,
                            current_message_text=sanitized_text,
                        )
                        classification_context_text = _classification_context_message_text(
                            source_user_text=classification_source_text,
                            current_message_text=sanitized_text,
                        )
                        request_type_decision = await self._decide_request_type(
                            current_draft=current_draft,
                            message_text=classification_context_text,
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
                            source_user_text=classification_source_text,
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
                                _CLASSIFICATION_SOURCE_USER_TEXT_KEY: classification_source_text,
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
                                    message_text=classification_context_text,
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
                            (
                                current_draft,
                                reply_text,
                            ) = await self._run_product_resolution_if_applicable(
                                payload=payload,
                                draft_repo=draft_repo,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                language=operator.language,
                                classification_intent=classification.intent,
                                request_type=request_type_decision.request_type,
                                message_text=classification_context_text,
                                reply_text=reply_text,
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
                            # --- Product discovery (text-based search) ---
                            # After EAN enrichment, try text-based product discovery
                            # if the draft still lacks a photo and we have info to search.
                            if (
                                reply_text is None
                                and current_draft.photo_url is None
                                and current_draft.request_type == AdRequestType.SINGLE_PRODUCT
                            ):
                                current_draft = await self._discover_product_for_draft(
                                    payload=payload,
                                    draft_repo=draft_repo,
                                    audit_repo=audit_repo,
                                    current_draft=current_draft,
                                    language=operator.language,
                                    message_text=sanitized_text,
                                    extracted_fields=extracted_fields,
                                )
                            if (
                                reply_text is None
                                and classification.intent == Intent.CREATE_AD
                                and current_draft.request_type == AdRequestType.SINGLE_PRODUCT
                                and current_draft.awaiting_product_confirmation
                                and current_draft.photo_url is not None
                            ):
                                confirmation_prompt = (
                                    await self._build_product_confirmation_requested_response(
                                        payload=payload,
                                        draft_repo=draft_repo,
                                        audit_repo=audit_repo,
                                        current_draft=current_draft,
                                        language=operator.language,
                                    )
                                )
                                current_draft = confirmation_prompt.draft
                                deterministic_action = confirmation_prompt.deterministic_action
                                generated_image_url = confirmation_prompt.generated_image_url
                                reply_text = confirmation_prompt.reply_text
                                action_buttons_prompt = confirmation_prompt.action_buttons_prompt
                                action_buttons = confirmation_prompt.action_buttons
                                pending_question_type = confirmation_prompt.pending_question_type
                                pending_question_context = (
                                    confirmation_prompt.pending_question_context
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

                    readiness_context = (
                        clarification_budget_context_override
                        if clarification_budget_context_override is not None
                        else pending_question_context
                    )
                    planner_allowed = _can_run_creative_brief_planner(current_draft=current_draft)

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
                        if planner_allowed and brief_instruction_override is None:
                            planner_turn = await self._advance_creative_brief_planner(
                                payload=payload,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                operator=operator,
                                history=history,
                                pending_question_context=readiness_context,
                                conversation_session=session_obj,
                                source_intent=classification.intent,
                                latest_user_message=sanitized_text,
                            )
                            llm_used = self._llm_gateway.uses_external_llm or llm_used
                            pending_question_type = planner_turn.pending_question_type
                            pending_question_context = planner_turn.pending_question_context
                            if planner_turn.reply_text is not None:
                                reply_text = planner_turn.reply_text
                                deterministic_action = "generation_gate_blocked"
                            else:
                                brief_instruction_override = (
                                    planner_turn.brief_instruction_text
                                    or brief_instruction_override
                                )
                                generation_result = await self._execute_generation(
                                    session=session,
                                    payload=payload,
                                    draft_repo=draft_repo,
                                    audit_repo=audit_repo,
                                    current_draft=current_draft,
                                    operator=operator,
                                    mode=mode,
                                    instruction_text=(brief_instruction_override or sanitized_text),
                                    followup_regen_requested=followup_regen_requested,
                                )
                                current_draft = generation_result.draft
                                reply_text = generation_result.reply_text
                                generated_image_url = generation_result.generated_image_url
                                variant_image_urls = generation_result.variant_image_urls
                                pending_question_type = generation_result.pending_question_type
                                pending_question_context = (
                                    generation_result.pending_question_context
                                )
                                publish_buttons_prompt = generation_result.publish_buttons_prompt
                                action_buttons_prompt = generation_result.action_buttons_prompt
                                action_buttons = generation_result.action_buttons
                                if generation_result.deterministic_action is not None:
                                    deterministic_action = generation_result.deterministic_action
                        elif planner_allowed:
                            generation_result = await self._execute_generation(
                                session=session,
                                payload=payload,
                                draft_repo=draft_repo,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                operator=operator,
                                mode=mode,
                                instruction_text=brief_instruction_override or sanitized_text,
                                followup_regen_requested=followup_regen_requested,
                            )
                            current_draft = generation_result.draft
                            reply_text = generation_result.reply_text
                            generated_image_url = generation_result.generated_image_url
                            variant_image_urls = generation_result.variant_image_urls
                            pending_question_type = generation_result.pending_question_type
                            pending_question_context = generation_result.pending_question_context
                            publish_buttons_prompt = generation_result.publish_buttons_prompt
                            action_buttons_prompt = generation_result.action_buttons_prompt
                            action_buttons = generation_result.action_buttons
                            if generation_result.deterministic_action is not None:
                                deterministic_action = generation_result.deterministic_action
                        elif _is_ready_for_generation(
                            current_draft,
                            operator=operator,
                            pending_question_context=readiness_context,
                        ):
                            generation_result = await self._execute_generation(
                                session=session,
                                payload=payload,
                                draft_repo=draft_repo,
                                audit_repo=audit_repo,
                                current_draft=current_draft,
                                operator=operator,
                                mode=mode,
                                instruction_text=sanitized_text,
                                followup_regen_requested=followup_regen_requested,
                            )
                            current_draft = generation_result.draft
                            reply_text = generation_result.reply_text
                            generated_image_url = generation_result.generated_image_url
                            variant_image_urls = generation_result.variant_image_urls
                            pending_question_type = generation_result.pending_question_type
                            pending_question_context = generation_result.pending_question_context
                            publish_buttons_prompt = generation_result.publish_buttons_prompt
                            action_buttons_prompt = generation_result.action_buttons_prompt
                            action_buttons = generation_result.action_buttons
                            if generation_result.deterministic_action is not None:
                                deterministic_action = generation_result.deterministic_action
                        else:
                            pre_generation_question = self._select_next_question(
                                current_draft=current_draft,
                                operator=operator,
                                after_preview_generation=False,
                                allow_regenerate_confirmation=False,
                                current_pending_question_context=readiness_context,
                            )
                            if pre_generation_question is not None:
                                pending_question_type = (
                                    pre_generation_question.pending_question_type
                                )
                                pending_question_context = (
                                    pre_generation_question.pending_question_context
                                )
                                reply_text = pre_generation_question.prompt_text
                                deterministic_action = "generation_gate_blocked"

                    if reply_text is None:
                        if (
                            classification.intent
                            in {
                                Intent.CREATE_AD,
                                Intent.REGENERATE_WITH_REFERENCE,
                                Intent.REGENERATE_FROM_SCRATCH,
                            }
                            and not planner_allowed
                            and not _is_ready_for_generation(
                                current_draft,
                                operator=operator,
                                pending_question_context=readiness_context,
                            )
                        ):
                            pre_generation_question = self._select_next_question(
                                current_draft=current_draft,
                                operator=operator,
                                after_preview_generation=False,
                                allow_regenerate_confirmation=False,
                                current_pending_question_context=readiness_context,
                            )
                            if pre_generation_question is not None:
                                pending_question_type = (
                                    pre_generation_question.pending_question_type
                                )
                                pending_question_context = (
                                    pre_generation_question.pending_question_context
                                )
                                reply_text = pre_generation_question.prompt_text
                                deterministic_action = (
                                    deterministic_action or "generation_gate_blocked"
                                )
                            else:
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
                pending_followup_question=question_policy.legacy_pending_followup_from_canonical(
                    pending_question_type=pending_question_type,
                    pending_question_context=pending_question_context,
                ),
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
                variant_image_urls=variant_image_urls,
                publish_buttons_prompt=publish_buttons_prompt,
                action_buttons_prompt=action_buttons_prompt,
                action_buttons=action_buttons,
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
        source_user_text: str,
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
                _CLASSIFICATION_SOURCE_USER_TEXT_KEY: source_user_text,
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
    ) -> tuple[AdDraft, str, bool]:
        if current_draft.rendered_image_url is None:
            if language.lower() == "he":
                return current_draft, "אין כרגע תמונת תצוגה מוכנה לפרסום.", False
            return current_draft, "There is no generated preview image ready for publishing.", False

        # Idempotency: skip CMS call if draft is already published.
        if current_draft.status == AdDraftStatus.PUBLISHED:
            existing = await published_repo.get_by_draft_id(current_draft.id)
            if existing is not None:
                await audit_repo.log(
                    actor="system",
                    action="publish_skipped_already_published",
                    operator_phone=payload.operator_phone,
                    metadata={
                        "wamid": payload.wamid,
                        "draft_id": str(current_draft.id),
                        "cms_id": existing.cms_id,
                    },
                )
                if language.lower() == "he":
                    return current_draft, "המודעה הזו כבר פורסמה.", True
                return current_draft, "This ad has already been published.", True

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
            return current_draft, _cms_not_connected_reply(), False

        if not self._cms_publisher.enabled:
            if language.lower() == "he":
                return current_draft, "הפרסום ל-CMS לא מוגדר כרגע במערכת.", False
            return current_draft, "CMS publishing is not configured yet.", False

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
                return current_draft, "הפרסום הצליח. המודעה נוספה לפלייליסט.", True
            return current_draft, "Publishing succeeded. Your ad was added to the playlist.", True
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
                return current_draft, f"הפרסום נכשל: {exc}", False
            return current_draft, f"Publishing failed: {exc}", False

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

    def _select_next_question(
        self,
        *,
        current_draft: AdDraft,
        operator: Operator,
        after_preview_generation: bool,
        allow_regenerate_confirmation: bool,
        current_pending_question_context: dict[str, Any] | None = None,
    ) -> question_policy.QuestionSelection | None:
        return question_policy.select_next_question(
            request_type=current_draft.request_type,
            classification_resolved=current_draft.is_classification_resolved,
            awaiting_product_confirmation=current_draft.awaiting_product_confirmation,
            has_product_name=current_draft.product_name is not None,
            has_price=current_draft.price is not None,
            has_store_type=_normalize_brand_value(operator.store_type) is not None,
            has_creative_guidance=_normalize_brand_value(operator.creative_guidance) is not None,
            language=operator.language,
            after_preview_generation=after_preview_generation,
            allow_regenerate_confirmation=allow_regenerate_confirmation,
            clarification_question_count=question_policy.clarification_count_from_context(
                pending_question_context=current_pending_question_context
            ),
        )

    async def _advance_creative_brief_planner(
        self,
        *,
        payload: InboundTaskPayload,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        operator: Operator,
        history: list[dict[str, str]],
        pending_question_context: dict[str, Any] | None,
        conversation_session: ConversationSession | None,
        source_intent: Intent | str | None,
        latest_user_message: str | None,
    ) -> _CreativeBriefPlannerTurnResult:
        context_payload = pending_question_context or {}
        session_payload = (
            context_payload.get(creative_brief_planner.SESSION_CONTEXT_KEY)
            if isinstance(context_payload, dict)
            else None
        )
        session_state: creative_brief_planner.CreativeBriefSessionState
        if _is_creative_brief_pending(
            pending_question_type=PendingQuestionType.MISSING_INFO,
            pending_question_context=context_payload,
        ) and isinstance(session_payload, dict):
            session_state = creative_brief_planner.CreativeBriefSessionState.model_validate(
                session_payload
            )
        else:
            confirmed_product = {
                "product_name": current_draft.product_name,
                "brand": current_draft.product_brand or current_draft.enriched_brand,
                "category": current_draft.enriched_category,
                "image_url": current_draft.photo_url or current_draft.enriched_image_url,
                "retailer_title": current_draft.enriched_description,
                "ean": current_draft.ean,
            }
            user_memory_context = {
                "business_name": operator.business_name,
                "store_type": operator.store_type,
                "creative_guidance": operator.creative_guidance,
                "brand_colors": operator.brand_colors or [],
                "logo_url": operator.logo_url,
            }
            planner_history_window = 5
            planner_history_item_max_chars = 180
            history_context: list[str] = []
            for item in history[-planner_history_window:]:
                role = item.get("role", "unknown")
                text = _truncate(item.get("text"), planner_history_item_max_chars)
                if text is not None:
                    history_context.append(f"{role}: {text}")
            source_intent_value = (
                source_intent.value if isinstance(source_intent, Intent) else source_intent
            )
            session_state = creative_brief_planner.initialize_session_state(
                confirmed_product=confirmed_product,
                user_memory_context=user_memory_context,
                conversation_context=history_context,
                source_intent=source_intent_value,
            )

        image_candidates = [current_draft.photo_url, current_draft.enriched_image_url]
        confirmed_image_urls: list[str] = []
        for candidate in image_candidates:
            normalized = _truncate(candidate, 2000)
            if normalized is None or normalized in confirmed_image_urls:
                continue
            confirmed_image_urls.append(normalized)
        refreshed_confirmed_product = dict(session_state.confirmed_product or {})
        refreshed_confirmed_product["product_name"] = (
            current_draft.product_name or refreshed_confirmed_product.get("product_name")
        )
        refreshed_confirmed_product["brand"] = (
            current_draft.product_brand
            or current_draft.enriched_brand
            or refreshed_confirmed_product.get("brand")
        )
        refreshed_confirmed_product["category"] = (
            current_draft.enriched_category or refreshed_confirmed_product.get("category")
        )
        refreshed_confirmed_product["retailer_title"] = (
            current_draft.enriched_description or refreshed_confirmed_product.get("retailer_title")
        )
        refreshed_confirmed_product["ean"] = current_draft.ean or refreshed_confirmed_product.get(
            "ean"
        )
        if confirmed_image_urls:
            refreshed_confirmed_product["image_url"] = confirmed_image_urls[0]
            refreshed_confirmed_product["image_urls"] = confirmed_image_urls
            refreshed_confirmed_product["discovered_image_url"] = current_draft.enriched_image_url
        session_state.confirmed_product = refreshed_confirmed_product

        question_count_from_session = max(
            _creative_brief_question_count_from_pending_context(
                conversation_session.pending_question_context
                if conversation_session is not None
                else None
            ),
            _creative_brief_question_count_from_pending_context(context_payload),
            max(0, session_state.question_count),
        )
        session_state.question_count = question_count_from_session
        missing_mandatory_fields = question_policy.missing_mandatory_fields(
            request_type=current_draft.request_type,
            has_product_name=current_draft.product_name is not None,
            has_price=current_draft.price is not None,
            has_store_type=_normalize_brand_value(operator.store_type) is not None,
            has_creative_guidance=_normalize_brand_value(operator.creative_guidance) is not None,
        )
        if missing_mandatory_fields:
            missing_key = missing_mandatory_fields[0]
            clarification_count = question_policy.clarification_count_from_context(
                pending_question_context=context_payload,
            )
            pending_context = {
                question_policy.QUESTION_CONTEXT_KEY: missing_key,
                question_policy.QUESTION_CONTEXT_REQUIRED: True,
                question_policy.QUESTION_CONTEXT_PHASE: (
                    question_policy.QUESTION_PHASE_PRE_GENERATION
                ),
                question_policy.QUESTION_CONTEXT_REPROMPT_COUNT: 0,
                question_policy.QUESTION_CONTEXT_CLARIFICATION_COUNT: clarification_count + 1,
            }
            await audit_repo.log(
                actor="system",
                action="creative_brief_planner_mandatory_short_circuit",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "question_key": missing_key,
                    "question_count": question_count_from_session,
                },
            )
            return _CreativeBriefPlannerTurnResult(
                pending_question_type=PendingQuestionType.MISSING_INFO,
                pending_question_context=pending_context,
                reply_text=_mandatory_short_circuit_question(
                    question_key=missing_key,
                    language=operator.language,
                ),
                brief_instruction_text=None,
                resume_intent=None,
                forced_reason="mandatory_short_circuit",
                validation_fallback=False,
            )

        source_intent_value = (
            source_intent.value if isinstance(source_intent, Intent) else source_intent
        )
        if source_intent_value is not None and source_intent_value != Intent.UNKNOWN.value:
            session_state.source_intent = source_intent_value

        planner_context = creative_brief_planner.CreativeBriefPlannerContext(
            language=operator.language,
            source_intent=session_state.source_intent,
            latest_user_message=latest_user_message,
            session_state=session_state,
        )
        # Release any pending DB write lock before waiting on external LLM I/O.
        await audit_repo.session.commit()
        planner_method = getattr(self._llm_gateway, "plan_creative_brief", None)
        planner_output = creative_brief_planner.noop_plan_creative_brief(context=planner_context)
        planner_call_failed = False
        planner_failure_type: str | None = None
        planner_failure_message: str | None = None
        if callable(planner_method):
            try:
                planner_output = await planner_method(
                    context=planner_context,
                    questions_asked_so_far=question_count_from_session,
                    missing_mandatory_fields=missing_mandatory_fields,
                )
            except Exception as exc:
                planner_call_failed = True
                planner_failure_type = exc.__class__.__name__
                planner_failure_message = _truncate(str(exc), 300)
                logger.warning(
                    "Creative brief planner failed; falling back to noop planner "
                    "(operator_phone=%s, wamid=%s)",
                    payload.operator_phone,
                    payload.wamid,
                    exc_info=True,
                )

        if planner_call_failed:
            await audit_repo.log(
                actor="system",
                action="creative_brief_planner_validation_fallback",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "reason": "planner_call_failed",
                    "error_type": planner_failure_type,
                    "error_message": planner_failure_message,
                    "question_count": session_state.question_count,
                },
            )

        resolution = creative_brief_planner.apply_planner_output(
            session_state=session_state,
            planner_output=planner_output,
            latest_user_message=latest_user_message,
            language=operator.language,
            question_cap=creative_brief_planner.DEFAULT_SOFT_QUESTION_CAP,
        )
        resolved_state = resolution.state
        if resolved_state.is_brief_ready:
            final_brief = resolved_state.final_brief
            if final_brief is None:
                final_brief = creative_brief_planner.synthesize_final_brief(
                    resolved_state,
                    confidence=planner_output.confidence,
                )
            instruction_text = creative_brief_planner.render_brief_instruction(final_brief)
            resume_intent = _intent_from_value(resolved_state.source_intent)
            action = "creative_brief_planner_brief_ready"
            if resolution.forced_reason == "cap":
                action = "creative_brief_planner_forced_ready_cap"
            elif resolution.validation_fallback:
                action = "creative_brief_planner_validation_fallback"
            await audit_repo.log(
                actor="system",
                action=action,
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "missing_dimensions": resolved_state.missing_dimensions,
                    "question_count": resolved_state.question_count,
                    "confidence": final_brief.confidence,
                },
            )
            return _CreativeBriefPlannerTurnResult(
                pending_question_type=PendingQuestionType.NONE,
                pending_question_context={},
                reply_text=None,
                brief_instruction_text=instruction_text,
                resume_intent=resume_intent,
                forced_reason=resolution.forced_reason,
                validation_fallback=resolution.validation_fallback,
            )

        next_question = resolved_state.pending_question
        if next_question is None:
            # Deterministic fallback if the planner returned an inconsistent state.
            fallback_output = creative_brief_planner.noop_plan_creative_brief(
                context=planner_context
            )
            fallback_resolution = creative_brief_planner.apply_planner_output(
                session_state=resolved_state,
                planner_output=fallback_output,
                latest_user_message=None,
                language=operator.language,
                question_cap=creative_brief_planner.DEFAULT_SOFT_QUESTION_CAP,
            )
            resolved_state = fallback_resolution.state
            next_question = resolved_state.pending_question
            resolution = fallback_resolution
            await audit_repo.log(
                actor="system",
                action="creative_brief_planner_validation_fallback",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "reason": "missing_pending_question",
                    "question_count": resolved_state.question_count,
                },
            )
        if next_question is None:
            # Final safety net: force ready with synthesized brief.
            forced_brief = creative_brief_planner.synthesize_final_brief(
                resolved_state,
                confidence=0.4,
            )
            instruction_text = creative_brief_planner.render_brief_instruction(forced_brief)
            return _CreativeBriefPlannerTurnResult(
                pending_question_type=PendingQuestionType.NONE,
                pending_question_context={},
                reply_text=None,
                brief_instruction_text=instruction_text,
                resume_intent=_intent_from_value(resolved_state.source_intent),
                forced_reason="validation_fallback",
                validation_fallback=True,
            )

        if resolved_state.question_count <= question_count_from_session:
            resolved_state.question_count = question_count_from_session + 1

        pending_context = {
            "stage": creative_brief_planner.STAGE_NAME,
            creative_brief_planner.SESSION_CONTEXT_KEY: resolved_state.model_dump(mode="json"),
        }
        action = "creative_brief_planner_question_asked"
        if resolution.validation_fallback:
            action = "creative_brief_planner_validation_fallback"
        await audit_repo.log(
            actor="system",
            action=action,
            operator_phone=payload.operator_phone,
            metadata={
                "wamid": payload.wamid,
                "draft_id": str(current_draft.id),
                "question_key": next_question.key,
                "missing_dimensions": resolved_state.missing_dimensions,
                "question_count": resolved_state.question_count,
                "confidence": planner_output.confidence,
            },
        )
        return _CreativeBriefPlannerTurnResult(
            pending_question_type=PendingQuestionType.MISSING_INFO,
            pending_question_context=pending_context,
            reply_text=next_question.question_text,
            brief_instruction_text=None,
            resume_intent=None,
            forced_reason=resolution.forced_reason,
            validation_fallback=resolution.validation_fallback,
        )

    async def _resolve_generation_instruction_after_product_confirmation(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        operator: Operator,
    ) -> tuple[AdDraft, str, bool]:
        fallback_instruction = _product_confirmation_generation_instruction(operator.language)
        brief_llm_used = self._llm_gateway.uses_external_llm
        draft_fields = self._marketing_brief_draft_fields(current_draft)
        operator_fields = self._marketing_brief_operator_fields(operator)

        try:
            brief = await self._llm_gateway.build_marketing_brief(
                language=operator.language,
                draft_fields=draft_fields,
                operator_fields=operator_fields,
            )
        except (LLMGatewayError, LLMSchemaError) as exc:
            await audit_repo.log(
                actor="system",
                action="marketing_brief_generation_failed",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "error": str(exc),
                },
            )
            return current_draft, fallback_instruction, brief_llm_used

        brief_payload = brief.model_dump(exclude_none=True)
        brief_text = brief.to_prompt_text().strip()
        if not brief_payload or not brief_text:
            await audit_repo.log(
                actor="system",
                action="marketing_brief_generation_empty",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                },
            )
            return current_draft, fallback_instruction, brief_llm_used

        updated_draft = await draft_repo.update_for_operator_with_version(
            draft_id=current_draft.id,
            operator_phone=payload.operator_phone,
            expected_version=current_draft.version,
            marketing_brief=brief_payload,
        )
        if updated_draft is None:
            await audit_repo.log(
                actor="system",
                action="marketing_brief_persist_skipped",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "reason": "stale_version",
                },
            )
        else:
            current_draft = updated_draft

        await audit_repo.log(
            actor="system",
            action="marketing_brief_generated",
            operator_phone=payload.operator_phone,
            metadata={
                "wamid": payload.wamid,
                "draft_id": str(current_draft.id),
                "brief_keys": sorted(brief_payload.keys()),
            },
        )
        instruction_text = f"{fallback_instruction}\n{brief_text}"
        return current_draft, instruction_text, brief_llm_used

    def _marketing_brief_draft_fields(self, draft: AdDraft) -> dict[str, Any]:
        return {
            "product_name": draft.product_name,
            "product_brand": draft.product_brand,
            "price": str(draft.price) if draft.price is not None else None,
            "currency": draft.currency,
            "promo_text": draft.promo_text,
            "ean": draft.ean,
            "photo_url": draft.photo_url,
            "enriched_brand": draft.enriched_brand,
            "enriched_category": draft.enriched_category,
            "enriched_description": draft.enriched_description,
            "enriched_image_url": draft.enriched_image_url,
        }

    def _marketing_brief_operator_fields(self, operator: Operator) -> dict[str, Any]:
        return {
            "business_name": operator.business_name,
            "store_type": operator.store_type,
            "creative_guidance": operator.creative_guidance,
            "brand_colors": operator.brand_colors,
            "logo_url": operator.logo_url,
            "language": operator.language,
            "currency": operator.currency,
        }

    async def _execute_generation(
        self,
        *,
        session: AsyncSession,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        operator: Operator,
        mode: GenerationMode,
        instruction_text: str,
        followup_regen_requested: bool,
    ) -> _GenerationExecutionResult:
        await self._send_generation_in_progress(
            to_phone=payload.operator_phone,
            language=operator.language,
        )
        try:
            draft_input = _to_generation_draft_input(
                draft=current_draft,
                operator=operator,
            )
            submissions = await self._ad_generation_service.submit_variant_pair(
                draft=draft_input,
                mode=mode,
                instruction_text=instruction_text,
                wamid=payload.wamid,
                width=self._render_width,
                height=self._render_height,
            )
            primary_job_id = submissions[0].job_id
            updated_draft = await draft_repo.update_for_operator_with_version(
                draft_id=current_draft.id,
                operator_phone=payload.operator_phone,
                expected_version=current_draft.version,
                status=AdDraftStatus.GENERATING,
                generation_job_id=primary_job_id,
                awaiting_product_confirmation=False,
            )
            if updated_draft is None:
                await audit_repo.log(
                    actor="system",
                    action="draft_stale_write_detected",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
                return _GenerationExecutionResult(
                    draft=current_draft,
                    reply_text="This draft was already changed. Please refresh and try again.",
                    generated_image_url=None,
                    variant_image_urls=None,
                    pending_question_type=PendingQuestionType.NONE,
                    pending_question_context={},
                    publish_buttons_prompt=None,
                    action_buttons_prompt=None,
                    action_buttons=None,
                    deterministic_action=None,
                )

            current_draft = updated_draft

            # Create variant round with two pending variant slots.
            # Use replace_active_round to supersede any existing active round.
            round_repo = AdVariantRoundRepository(session)
            existing_rounds = await round_repo.list_by_draft_id(current_draft.id)
            attempt_no = (existing_rounds[0].attempt_no + 1) if existing_rounds else 1
            variant_defs = [
                {
                    "slot_no": slot + 1,
                    "status": AdVariantStatus.FAILED,
                    "prompt_snapshot": submissions[slot].request_payload.get("prompt", ""),
                }
                for slot in range(2)
            ]
            if existing_rounds:
                variant_round = await round_repo.replace_active_round(
                    draft_id=current_draft.id,
                    attempt_no=attempt_no,
                    variants=variant_defs,
                )
            else:
                variant_round = await round_repo.create(
                    draft_id=current_draft.id,
                    attempt_no=attempt_no,
                    status=AdVariantRoundStatus.ACTIVE,
                    variants=variant_defs,
                )

            await audit_repo.log(
                actor="system",
                action="generation_variant_pair_submitted",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "round_id": str(variant_round.id),
                    "job_ids": [s.job_id for s in submissions],
                    "mode": mode.value,
                },
            )
            logger.info(
                "Variant pair submitted "
                "(wamid=%s, operator_phone=%s, draft_id=%s, "
                "round_id=%s, job_ids=%s, mode=%s)",
                payload.wamid,
                payload.operator_phone,
                current_draft.id,
                variant_round.id,
                [s.job_id for s in submissions],
                mode.value,
            )

            # Capture variant info before commit expires ORM objects.
            round_id = variant_round.id

            # Commit now so the DB lock is released while we wait for generation.
            await session.commit()
            poll_results = await self._ad_generation_service.poll_variant_pair(
                submissions=submissions,
            )

            # Update each variant with poll results.
            variant_image_urls: list[str] = []
            all_succeeded = True
            refreshed_variants = await AdVariantRepository(session).list_by_round_id(round_id)
            for slot_idx, poll_result in enumerate(poll_results):
                slot_no = slot_idx + 1
                variant = next(
                    (v for v in refreshed_variants if v.slot_no == slot_no),
                    None,
                )
                if variant is None:
                    all_succeeded = False
                    continue
                if (
                    poll_result.status == NanoBananaJobStatus.COMPLETED
                    and poll_result.output_image_url
                ):
                    variant.status = AdVariantStatus.VALID
                    variant.image_url = poll_result.output_image_url
                    variant_image_urls.append(poll_result.output_image_url)
                else:
                    variant.status = AdVariantStatus.FAILED
                    variant.error_code = poll_result.error_code
                    variant.error_message = poll_result.error_message
                    all_succeeded = False

            if all_succeeded and len(variant_image_urls) == 2:
                primary_image_url = variant_image_urls[0]
                completed_draft = await draft_repo.update_for_operator_with_version(
                    draft_id=current_draft.id,
                    operator_phone=payload.operator_phone,
                    expected_version=current_draft.version,
                    status=AdDraftStatus.PREVIEW_READY,
                    rendered_image_url=primary_image_url,
                    preview_reference_url=primary_image_url,
                    awaiting_product_confirmation=False,
                )
                if completed_draft is None:
                    await audit_repo.log(
                        actor="system",
                        action="draft_stale_write_detected",
                        operator_phone=payload.operator_phone,
                        metadata={"wamid": payload.wamid},
                    )
                    return _GenerationExecutionResult(
                        draft=current_draft,
                        reply_text="This draft was already changed. Please refresh and try again.",
                        generated_image_url=None,
                        variant_image_urls=None,
                        pending_question_type=PendingQuestionType.NONE,
                        pending_question_context={},
                        publish_buttons_prompt=None,
                        action_buttons_prompt=None,
                        action_buttons=None,
                        deterministic_action=None,
                    )

                current_draft = completed_draft
                await audit_repo.log(
                    actor="system",
                    action="generation_variant_pair_completed",
                    operator_phone=payload.operator_phone,
                    metadata={
                        "wamid": payload.wamid,
                        "draft_id": str(current_draft.id),
                        "round_id": str(round_id),
                        "variant_image_urls": variant_image_urls,
                    },
                )
                logger.info(
                    "Variant pair completed "
                    "(wamid=%s, operator_phone=%s, draft_id=%s, "
                    "round_id=%s, variants=%d)",
                    payload.wamid,
                    payload.operator_phone,
                    current_draft.id,
                    round_id,
                    len(variant_image_urls),
                )
                reply_text = _generation_completed_reply(operator.language)

                return _GenerationExecutionResult(
                    draft=current_draft,
                    reply_text=reply_text,
                    generated_image_url=primary_image_url,
                    variant_image_urls=variant_image_urls,
                    pending_question_type=PendingQuestionType.VARIANT_SELECTION,
                    pending_question_context={"round_id": str(round_id)},
                    publish_buttons_prompt=None,
                    action_buttons_prompt=_variant_selection_prompt(operator.language),
                    action_buttons=_variant_selection_buttons(operator.language),
                    deterministic_action="generation_completed",
                )

            # At least one variant failed — mark round as failed.
            refreshed_round = await round_repo.get_by_id(round_id)
            if refreshed_round is not None:
                refreshed_round.status = AdVariantRoundStatus.FAILED
                refreshed_round.failure_reason = "one or more variants failed generation"
            failed_draft = await draft_repo.update_for_operator_with_version(
                draft_id=current_draft.id,
                operator_phone=payload.operator_phone,
                expected_version=current_draft.version,
                status=AdDraftStatus.DRAFT,
                awaiting_product_confirmation=False,
            )
            if failed_draft is not None:
                current_draft = failed_draft
            await audit_repo.log(
                actor="system",
                action="generation_variant_pair_failed",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "round_id": str(round_id),
                    "succeeded_count": len(variant_image_urls),
                },
            )
            logger.warning(
                "Variant pair failed "
                "(wamid=%s, operator_phone=%s, draft_id=%s, "
                "round_id=%s, succeeded=%d/2)",
                payload.wamid,
                payload.operator_phone,
                current_draft.id,
                round_id,
                len(variant_image_urls),
            )
            return _GenerationExecutionResult(
                draft=current_draft,
                reply_text=_generation_failed_reply(operator.language),
                generated_image_url=None,
                variant_image_urls=None,
                pending_question_type=PendingQuestionType.NONE,
                pending_question_context={},
                publish_buttons_prompt=None,
                action_buttons_prompt=None,
                action_buttons=None,
                deterministic_action="generation_failed",
            )
        except AdGenerationError as exc:
            if current_draft.status == AdDraftStatus.GENERATING:
                reverted_draft = await draft_repo.update_for_operator_with_version(
                    draft_id=current_draft.id,
                    operator_phone=payload.operator_phone,
                    expected_version=current_draft.version,
                    status=AdDraftStatus.DRAFT,
                    awaiting_product_confirmation=False,
                )
                if reverted_draft is not None:
                    current_draft = reverted_draft
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
                "Generation flow failed (wamid=%s, operator_phone=%s, draft_id=%s, mode=%s)",
                payload.wamid,
                payload.operator_phone,
                current_draft.id,
                mode.value,
            )
            return _GenerationExecutionResult(
                draft=current_draft,
                reply_text=_generation_failed_reply(operator.language),
                generated_image_url=None,
                variant_image_urls=None,
                pending_question_type=PendingQuestionType.NONE,
                pending_question_context={},
                publish_buttons_prompt=None,
                action_buttons_prompt=None,
                action_buttons=None,
                deterministic_action="generation_failed",
            )

    async def _resolve_pending_missing_info_answer(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        operator_repo: OperatorRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        operator: Operator,
        history: list[dict[str, str]],
        pending_question_context: dict[str, Any],
        message_text: str,
    ) -> tuple[
        AdDraft,
        question_policy.PendingResolutionStatus,
        str | None,
        bool,
    ]:
        llm_used = False
        question_key = question_policy.pending_question_key(
            pending_question_type=PendingQuestionType.MISSING_INFO,
            pending_question_context=pending_question_context,
        )
        if question_key == question_policy.QUESTION_KEY_PRICE:
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
                    question_policy.PendingResolutionStatus.UNRESOLVED,
                    None,
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
                    question_policy.PendingResolutionStatus.UNRESOLVED,
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
            return (
                current_draft,
                question_policy.PendingResolutionStatus.RESOLVED,
                None,
                llm_used,
            )
        if question_key == question_policy.QUESTION_KEY_STORE_TYPE:
            store_type = _truncate(message_text, 120)
            if store_type is None:
                return (
                    current_draft,
                    question_policy.PendingResolutionStatus.UNRESOLVED,
                    None,
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
            return (
                current_draft,
                question_policy.PendingResolutionStatus.RESOLVED,
                None,
                llm_used,
            )
        if question_key == question_policy.QUESTION_KEY_CREATIVE_GUIDANCE:
            creative_guidance = _truncate(message_text, 500)
            if creative_guidance is None:
                return (
                    current_draft,
                    question_policy.PendingResolutionStatus.UNRESOLVED,
                    None,
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
            return (
                current_draft,
                question_policy.PendingResolutionStatus.RESOLVED,
                None,
                llm_used,
            )
        if question_key == question_policy.QUESTION_KEY_PRODUCT_NAME:
            product_name = _truncate(message_text, 120)
            if product_name is None:
                return (
                    current_draft,
                    question_policy.PendingResolutionStatus.UNRESOLVED,
                    None,
                    llm_used,
                )
            updated_draft = await draft_repo.update_for_operator_with_version(
                draft_id=current_draft.id,
                operator_phone=payload.operator_phone,
                expected_version=current_draft.version,
                product_name=product_name,
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
                    question_policy.PendingResolutionStatus.UNRESOLVED,
                    "This draft was already changed. Please refresh and try again.",
                    llm_used,
                )
            current_draft = updated_draft
            await audit_repo.log(
                actor="system",
                action="missing_info_product_name_captured",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "product_name": current_draft.product_name,
                },
            )
            return (
                current_draft,
                question_policy.PendingResolutionStatus.RESOLVED,
                None,
                llm_used,
            )
        return (
            current_draft,
            question_policy.PendingResolutionStatus.UNRESOLVED,
            None,
            llm_used,
        )

    async def _prepare_product_confirmation_image(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
    ) -> tuple[AdDraft, str | None]:
        photo_url = _truncate(current_draft.photo_url, 2000)
        if photo_url is None:
            return current_draft, None

        if _is_confirmation_image_url_compatible(photo_url):
            return current_draft, photo_url

        ingest_external = getattr(self._operator_photo_ingestor, "ingest_external_image_url", None)
        if not callable(ingest_external):
            return current_draft, None

        try:
            ingested_photo = await ingest_external(image_url=photo_url)
        except MediaIngestError as exc:
            await audit_repo.log(
                actor="system",
                action="product_confirmation_image_rehost_failed",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "photo_url": photo_url,
                    "error": str(exc),
                },
            )
            return current_draft, None

        updated_draft = await draft_repo.update_for_operator_with_version(
            draft_id=current_draft.id,
            operator_phone=payload.operator_phone,
            expected_version=current_draft.version,
            photo_url=ingested_photo.public_url,
        )
        if updated_draft is not None:
            current_draft = updated_draft

        await audit_repo.log(
            actor="system",
            action="product_confirmation_image_rehosted",
            operator_phone=payload.operator_phone,
            metadata={
                "wamid": payload.wamid,
                "draft_id": str(current_draft.id),
                "source_photo_url": photo_url,
                "rehosted_photo_url": ingested_photo.public_url,
            },
        )
        return current_draft, ingested_photo.public_url

    async def _build_product_confirmation_requested_response(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        language: str,
    ) -> _ProductConfirmationPromptResult:
        current_draft, confirmation_image_url = await self._prepare_product_confirmation_image(
            payload=payload,
            draft_repo=draft_repo,
            audit_repo=audit_repo,
            current_draft=current_draft,
        )
        reply_text = _product_confirmation_caption(
            language=language,
            product_name=current_draft.product_name,
        )
        if confirmation_image_url is None and current_draft.photo_url is not None:
            reply_text = f"{reply_text}\n\n" + _product_confirmation_image_link_fallback_reply(
                language=language,
                image_url=current_draft.photo_url,
            )
        action_buttons = [
            (
                BUTTON_CONFIRM_PRODUCT_SELECTION,
                _product_confirmation_accept_label(language),
            ),
            (
                BUTTON_REJECT_PRODUCT_SELECTION,
                _product_confirmation_reject_label(language),
            ),
        ]
        pending_question_context = {
            "draft_id": str(current_draft.id),
            "photo_url": current_draft.photo_url,
            "product_name": current_draft.product_name,
        }
        await audit_repo.log(
            actor="system",
            action="product_confirmation_requested",
            operator_phone=payload.operator_phone,
            metadata={
                "wamid": payload.wamid,
                "draft_id": str(current_draft.id),
                "photo_url": current_draft.photo_url,
                "product_name": current_draft.product_name,
            },
        )
        return _ProductConfirmationPromptResult(
            draft=current_draft,
            reply_text=reply_text,
            generated_image_url=confirmation_image_url,
            action_buttons_prompt=_product_confirmation_buttons_prompt(language),
            action_buttons=action_buttons,
            pending_question_type=PendingQuestionType.PRODUCT_CONFIRMATION,
            pending_question_context=pending_question_context,
            deterministic_action="product_confirmation_requested",
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

    async def _run_product_resolution_if_applicable(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        language: str,
        classification_intent: Intent,
        request_type: AdRequestType,
        message_text: str,
        reply_text: str | None,
    ) -> tuple[AdDraft, str | None]:
        if reply_text is not None:
            return current_draft, reply_text
        if classification_intent != Intent.CREATE_AD:
            return current_draft, reply_text
        if request_type != AdRequestType.SINGLE_PRODUCT:
            return current_draft, reply_text
        if not self._product_resolution_service.enabled:
            return current_draft, reply_text

        resolution = await self._product_resolution_service.resolve(
            message_text=message_text,
            language=language,
        )
        metadata: dict[str, Any] = {
            "wamid": payload.wamid,
            "draft_id": str(current_draft.id),
            "status": resolution.status,
            "product_query": resolution.product_query,
            "brand": resolution.brand,
        }
        if resolution.selected_result is not None:
            metadata.update(
                {
                    "source": resolution.selected_result.source,
                    "source_url": resolution.selected_result.product_url,
                    "search_method": resolution.selected_result.search_method,
                    "title": resolution.selected_result.title,
                }
            )
        await audit_repo.log(
            actor="system",
            action="product_resolution_attempted",
            operator_phone=payload.operator_phone,
            metadata=metadata,
        )
        logger.info(
            "Product resolution completed (wamid=%s, operator_phone=%s, status=%s, query=%s)",
            payload.wamid,
            payload.operator_phone,
            resolution.status,
            resolution.product_query,
        )
        if resolution.status == "resolved" and resolution.selected_result is not None:
            update_fields = self._build_product_resolution_update_fields(
                current_draft=current_draft,
                resolution=resolution,
            )
            if not update_fields:
                return current_draft, reply_text
            updated_draft = await draft_repo.update_for_operator_with_version(
                draft_id=current_draft.id,
                operator_phone=payload.operator_phone,
                expected_version=current_draft.version,
                **update_fields,
            )
            if updated_draft is None:
                stale_reply = "This draft was already changed. Please refresh and try again."
                await audit_repo.log(
                    actor="system",
                    action="draft_stale_write_detected",
                    operator_phone=payload.operator_phone,
                    metadata={"wamid": payload.wamid},
                )
                return current_draft, stale_reply
            return updated_draft, reply_text

        if (
            resolution.status == "needs_clarification"
            and current_draft.product_name is None
            and resolution.clarification_question is not None
        ):
            return current_draft, resolution.clarification_question
        return current_draft, reply_text

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

    async def _discover_product_for_draft(
        self,
        *,
        payload: InboundTaskPayload,
        draft_repo: AdDraftRepository,
        audit_repo: AuditEventRepository,
        current_draft: AdDraft,
        language: str,
        message_text: str,
        extracted_fields: ExtractedAdFields | None,
    ) -> AdDraft:
        """Search retailers + Serper for a product image. Returns updated draft."""
        # Build search query from already-extracted fields when possible
        # to avoid an extra LLM call.
        search_query = _build_discovery_search_query(extracted_fields)
        if not search_query:
            # Fall back to lightweight LLM extraction.
            try:
                product_query = await self._llm_gateway.extract_product_query(
                    message_text=message_text,
                    language=language,
                )
                search_query = product_query.to_search_query()
            except (LLMGatewayError, LLMSchemaError):
                logger.warning(
                    "Product query extraction failed (wamid=%s)",
                    payload.wamid,
                    exc_info=True,
                )
                return current_draft

        if not search_query:
            return current_draft

        result = await self._product_discovery_service.discover(query=search_query)

        if result.status == DiscoveryStatus.NOT_FOUND or result.product is None:
            await audit_repo.log(
                actor="system",
                action="product_discovery_not_found",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "query": search_query,
                },
            )
            return current_draft

        # Build update fields — never overwrite an operator-uploaded photo.
        update_fields: dict[str, Any] = {}
        if result.product.title:
            update_fields["enriched_description"] = _truncate(result.product.title, 500)
        if result.product.image_url:
            update_fields["enriched_image_url"] = _truncate(result.product.image_url, 2000)
        if current_draft.product_name is None and result.product.title:
            update_fields["product_name"] = _truncate(result.product.title, 120)
        if current_draft.photo_url is None and result.product.image_url:
            update_fields["photo_url"] = _truncate(result.product.image_url, 2000)
        if result.product.source:
            update_fields["enrichment_source"] = result.product.source

        if not update_fields:
            return current_draft

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
                action="product_discovery_applied",
                operator_phone=payload.operator_phone,
                metadata={
                    "wamid": payload.wamid,
                    "draft_id": str(current_draft.id),
                    "query": search_query,
                    "source": result.product.source,
                    "search_method": result.search_method,
                    "title": result.product.title,
                },
            )
        return current_draft

    def _build_product_resolution_update_fields(
        self,
        *,
        current_draft: AdDraft,
        resolution: ProductResolutionResult,
    ) -> dict[str, Any]:
        selected = resolution.selected_result
        if selected is None:
            return {}

        update_fields: dict[str, Any] = {
            "enrichment_source": _truncate(selected.source, 32) or "none",
            "enriched_description": _truncate(selected.description, 500),
            "enriched_image_url": _truncate(selected.image_url, 2000),
            "enrichment_unavailable_notified_at": None,
        }
        if current_draft.product_name is None:
            update_fields["product_name"] = _truncate(selected.title, 120)
        if current_draft.product_brand is None and resolution.brand is not None:
            update_fields["product_brand"] = _truncate(resolution.brand, 120)
        if current_draft.photo_url is None:
            resolved_photo = _truncate(selected.image_url, 2000)
            update_fields["photo_url"] = resolved_photo
            if resolved_photo is not None:
                update_fields["awaiting_product_confirmation"] = True
        if current_draft.promo_text is None and selected.description is not None:
            update_fields["promo_text"] = _truncate(selected.description, 240)

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


def _build_discovery_search_query(
    extracted_fields: ExtractedAdFields | None,
) -> str:
    """Combine already-extracted ad fields into a search query string."""
    if extracted_fields is None:
        return ""
    parts: list[str] = []
    if extracted_fields.product_brand:
        parts.append(extracted_fields.product_brand)
    if extracted_fields.product_name:
        parts.append(extracted_fields.product_name)
    return " ".join(parts)


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


def _intent_from_value(value: str | None) -> Intent | None:
    if value is None:
        return None
    try:
        return Intent(value)
    except ValueError:
        return None


def _is_creative_brief_pending(
    *,
    pending_question_type: PendingQuestionType,
    pending_question_context: dict[str, Any] | None,
) -> bool:
    if pending_question_type != PendingQuestionType.MISSING_INFO:
        return False
    context = pending_question_context or {}
    return context.get("stage") == creative_brief_planner.STAGE_NAME


def _creative_brief_question_count_from_pending_context(
    pending_question_context: dict[str, Any] | None,
) -> int:
    if not isinstance(pending_question_context, dict):
        return 0
    if pending_question_context.get("stage") != creative_brief_planner.STAGE_NAME:
        return 0
    session_payload = pending_question_context.get(creative_brief_planner.SESSION_CONTEXT_KEY)
    if not isinstance(session_payload, dict):
        return 0
    raw_count = session_payload.get("question_count")
    if isinstance(raw_count, int):
        return max(raw_count, 0)
    if isinstance(raw_count, str) and raw_count.isdigit():
        return int(raw_count)
    return 0


def _can_run_creative_brief_planner(*, current_draft: AdDraft) -> bool:
    return (
        current_draft.is_classification_resolved
        and current_draft.request_type != AdRequestType.UNSET
        and not current_draft.awaiting_product_confirmation
    )


def _is_ready_for_generation(
    draft: AdDraft,
    *,
    operator: Operator,
    pending_question_context: dict[str, Any] | None,
) -> bool:
    return question_policy.is_generation_ready(
        request_type=draft.request_type,
        classification_resolved=draft.is_classification_resolved,
        awaiting_product_confirmation=draft.awaiting_product_confirmation,
        has_product_name=draft.product_name is not None,
        has_price=draft.price is not None,
        has_store_type=_normalize_brand_value(operator.store_type) is not None,
        has_creative_guidance=_normalize_brand_value(operator.creative_guidance) is not None,
        clarification_question_count=question_policy.clarification_count_from_context(
            pending_question_context=pending_question_context
        ),
    )


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
        marketing_brief=draft.marketing_brief,
        enriched_image_url=draft.enriched_image_url,
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
        return "תרצה לפרסם את הגרסא שבחרת?\n(במידה ויש שינויים נוספים תכתוב אותם כאן)"
    return (
        "Would you like to publish the selected version?\n"
        "(If there are more changes, write them here.)"
    )


def _is_confirmation_image_url_compatible(image_url: str) -> bool:
    try:
        parsed = urlparse(image_url)
    except ValueError:
        return False
    if parsed.scheme.lower() != "https":
        return False
    host = parsed.netloc.strip().lower()
    if not host:
        return False
    if host in _CONFIRMATION_UNSAFE_IMAGE_HOSTS:
        return False
    path = parsed.path.lower()
    if path.endswith(_CONFIRMATION_SAFE_IMAGE_SUFFIXES):
        return True
    if "storage.googleapis.com" in host or host.endswith(".amazonaws.com"):
        return True
    return False


def _product_confirmation_image_link_fallback_reply(*, language: str, image_url: str) -> str:
    if language.lower() == "he":
        return f"לא הצלחתי לצרף את התמונה ישירות. אפשר לבדוק אותה כאן: {image_url}"
    return f"I could not attach the image directly. You can review it here: {image_url}"


def _product_confirmation_caption(language: str, product_name: str | None) -> str:
    resolved_name = (product_name or "").strip()
    if language.lower() == "he":
        if resolved_name:
            return f'מצאתי תמונה עבור "{resolved_name}". זה המוצר שהתכוונת אליו?'
        return "מצאתי תמונה למוצר. זה המוצר שהתכוונת אליו?"
    if resolved_name:
        return f'I found an image for "{resolved_name}". Is this the product you meant?'
    return "I found a product image. Is this the product you meant?"


def _product_confirmation_buttons_prompt(language: str) -> str:
    if language.lower() == "he":
        return "אשר/דחה את המוצר כדי שאמשיך לייצר את המודעה."
    return "Confirm or reject this product so I can continue generating the ad."


def _product_confirmation_accept_label(language: str) -> str:
    if language.lower() == "he":
        return "כן, זה המוצר"
    return "Yes, this one"


def _product_confirmation_reject_label(language: str) -> str:
    if language.lower() == "he":
        return "לא, מוצר אחר"
    return "No, different product"


def _product_confirmation_use_buttons_reply(language: str) -> str:
    if language.lower() == "he":
        return "כדי להמשיך, בחר באחד מכפתורי אישור המוצר."
    return "To continue, choose one of the product confirmation buttons."


def _product_confirmation_rejected_reply(language: str) -> str:
    if language.lower() == "he":
        return "בסדר, לא אשתמש בתמונה הזאת. כתוב לי שם מוצר מדויק יותר ואחפש שוב."
    return (
        "Okay, I will not use this image. "
        "Send a more specific product name and I will search again."
    )


def _product_confirmation_generation_instruction(language: str) -> str:
    if language.lower() == "he":
        return "המשתמש אישר את תמונת המוצר. צור מודעה חדשה."
    return "The user approved the product image. Generate a new ad."


def _publish_buttons_prompt(language: str) -> str:
    if language.lower() == "he":
        return "תרצה לפרסם את הגרסא שבחרת?\n(במידה ויש שינויים נוספים תכתוב אותם כאן)"
    return (
        "Would you like to publish the selected version?\n"
        "(If there are more changes, write them here.)"
    )


def _variant_selection_prompt(language: str) -> str:
    if language.lower() == "he":
        return "איזו גרסה אתה מעדיף?"
    return "Which variant do you prefer?"


def _variant_selection_buttons(
    language: str,
) -> list[tuple[str, str]]:
    if language.lower() == "he":
        return [
            (BUTTON_SELECT_VARIANT_A, "גרסה A"),
            (BUTTON_SELECT_VARIANT_B, "גרסה B"),
        ]
    return [
        (BUTTON_SELECT_VARIANT_A, "Variant A"),
        (BUTTON_SELECT_VARIANT_B, "Variant B"),
    ]


def _variant_selected_reply(language: str, slot_label: str) -> str:
    if language.lower() == "he":
        return f"גרסה {slot_label} נבחרה."
    return f"Variant {slot_label} selected."


def _publish_canceled_reply(language: str) -> str:
    if language.lower() == "he":
        return "המודעה לא פורסמה. אפסתי את הטיוטה ואפשר להתחיל מודעה חדשה מתי שתרצה."
    return "The ad was not published. I reset the draft and you can start a new ad anytime."


def _variant_selection_use_buttons_reply(language: str) -> str:
    if language.lower() == "he":
        return "אנא השתמש בכפתורים לבחירת הגרסה."
    return "Please use the buttons to select a variant."


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
