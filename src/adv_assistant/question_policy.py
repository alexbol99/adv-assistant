from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from adv_assistant.db.enums import AdRequestType, PendingQuestionType
from adv_assistant.llm_gateway import Intent

QUESTION_KEY_PRICE = "price"
QUESTION_KEY_STORE_TYPE = "store_type"
QUESTION_KEY_CREATIVE_GUIDANCE = "creative_guidance"
QUESTION_KEY_PRODUCT_NAME = "product_name"
QUESTION_KEY_REGENERATE_CONFIRMATION = "regenerate_confirmation"
QUESTION_CONTEXT_KEY = "question_key"
QUESTION_CONTEXT_REQUIRED = "required"
QUESTION_CONTEXT_PHASE = "phase"
QUESTION_CONTEXT_REPROMPT_COUNT = "reprompt_count"
QUESTION_CONTEXT_CLARIFICATION_COUNT = "clarification_count"
QUESTION_PHASE_PRE_GENERATION = "pre_generation"
QUESTION_PHASE_POST_PREVIEW = "post_preview"
MAX_REPROMPTS_DEFAULT = 2
MAX_PRE_GENERATION_CLARIFICATION_QUESTIONS = 3
_ALLOWED_MANDATORY_QUESTION_KEYS = frozenset(
    {
        QUESTION_KEY_PRODUCT_NAME,
        QUESTION_KEY_PRICE,
    }
)


@dataclass(frozen=True, slots=True)
class RequestTypeQuestionRules:
    required_before_generation: tuple[str, ...]
    optional_after_preview: tuple[str, ...]


REQUEST_TYPE_QUESTION_RULES: dict[AdRequestType, RequestTypeQuestionRules] = {
    AdRequestType.UNSET: RequestTypeQuestionRules(
        required_before_generation=(),
        optional_after_preview=(),
    ),
    AdRequestType.SINGLE_PRODUCT: RequestTypeQuestionRules(
        required_before_generation=(
            QUESTION_KEY_PRODUCT_NAME,
            QUESTION_KEY_PRICE,
        ),
        optional_after_preview=(
            QUESTION_KEY_STORE_TYPE,
            QUESTION_KEY_CREATIVE_GUIDANCE,
        ),
    ),
    AdRequestType.MULTI_PRODUCT: RequestTypeQuestionRules(
        required_before_generation=(),
        optional_after_preview=(
            QUESTION_KEY_STORE_TYPE,
            QUESTION_KEY_CREATIVE_GUIDANCE,
        ),
    ),
    AdRequestType.STORE_GENERAL: RequestTypeQuestionRules(
        required_before_generation=(),
        optional_after_preview=(
            QUESTION_KEY_STORE_TYPE,
            QUESTION_KEY_CREATIVE_GUIDANCE,
        ),
    ),
}


INTERRUPT_PENDING_QUESTION_INTENTS = {
    Intent.CREATE_AD,
    Intent.REGENERATE_WITH_REFERENCE,
    Intent.REGENERATE_FROM_SCRATCH,
    Intent.PUBLISH_AD,
    Intent.DELETE_ALL,
    Intent.LIST_ADS,
}


@dataclass(frozen=True, slots=True)
class QuestionSelection:
    pending_question_type: PendingQuestionType
    pending_question_context: dict[str, Any]
    prompt_text: str
    is_blocking_for_generation: bool


class PendingResolutionStatus(StrEnum):
    RESOLVED = "resolved"
    UNRESOLVED = "unresolved"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True, slots=True)
class PendingQuestionTransition:
    status: PendingResolutionStatus
    pending_question_type: PendingQuestionType
    pending_question_context: dict[str, Any]
    reply_text: str | None
    exhausted_reprompts: bool = False


def pending_question_key(
    *,
    pending_question_type: PendingQuestionType,
    pending_question_context: dict[str, Any] | None,
) -> str | None:
    if pending_question_type not in {
        PendingQuestionType.MISSING_INFO,
        PendingQuestionType.GENERATION_RETRY,
    }:
        return None
    context = pending_question_context or {}
    question_key = context.get(QUESTION_CONTEXT_KEY)
    if isinstance(question_key, str) and question_key.strip():
        return question_key
    return None


def is_generation_ready(
    *,
    request_type: AdRequestType,
    classification_resolved: bool,
    awaiting_product_confirmation: bool,
    has_product_name: bool,
    has_price: bool = False,
    has_store_type: bool = False,
    has_creative_guidance: bool = False,
    clarification_question_count: int = 0,
    max_pre_generation_clarification_questions: int = MAX_PRE_GENERATION_CLARIFICATION_QUESTIONS,
) -> bool:
    if not classification_resolved:
        return False
    if awaiting_product_confirmation:
        return False
    asked_clarifications = max(clarification_question_count, 0)
    for required_key in _required_before_generation_keys(request_type=request_type):
        if _is_missing(
            question_key=required_key,
            has_product_name=has_product_name,
            has_price=has_price,
            has_store_type=has_store_type,
            has_creative_guidance=has_creative_guidance,
        ):
            return asked_clarifications >= max_pre_generation_clarification_questions
    return True


def clarification_count_from_context(
    *,
    pending_question_context: dict[str, Any] | None,
) -> int:
    context = pending_question_context or {}
    return _parse_reprompt_count(context.get(QUESTION_CONTEXT_CLARIFICATION_COUNT))


def missing_mandatory_fields(
    *,
    request_type: AdRequestType,
    has_product_name: bool,
    has_price: bool,
    has_store_type: bool,
    has_creative_guidance: bool,
) -> list[str]:
    missing: list[str] = []
    for required_key in _required_before_generation_keys(request_type=request_type):
        if _is_missing(
            question_key=required_key,
            has_product_name=has_product_name,
            has_price=has_price,
            has_store_type=has_store_type,
            has_creative_guidance=has_creative_guidance,
        ):
            missing.append(required_key)
    return missing


def select_next_question(
    *,
    request_type: AdRequestType,
    classification_resolved: bool,
    awaiting_product_confirmation: bool,
    has_product_name: bool,
    has_price: bool,
    has_store_type: bool,
    has_creative_guidance: bool,
    language: str,
    after_preview_generation: bool,
    allow_regenerate_confirmation: bool = False,
    clarification_question_count: int = 0,
    max_pre_generation_clarification_questions: int = MAX_PRE_GENERATION_CLARIFICATION_QUESTIONS,
) -> QuestionSelection | None:
    if not classification_resolved or request_type == AdRequestType.UNSET:
        return QuestionSelection(
            pending_question_type=PendingQuestionType.CLASSIFICATION,
            pending_question_context={},
            prompt_text=classification_prompt(language),
            is_blocking_for_generation=True,
        )

    if awaiting_product_confirmation:
        return QuestionSelection(
            pending_question_type=PendingQuestionType.PRODUCT_CONFIRMATION,
            pending_question_context={},
            prompt_text=product_confirmation_use_buttons_prompt(language),
            is_blocking_for_generation=True,
        )

    asked_clarifications = max(clarification_question_count, 0)
    for required_key in _required_before_generation_keys(request_type=request_type):
        if _is_missing(
            question_key=required_key,
            has_product_name=has_product_name,
            has_price=has_price,
            has_store_type=has_store_type,
            has_creative_guidance=has_creative_guidance,
        ):
            if asked_clarifications >= max_pre_generation_clarification_questions:
                return None
            return QuestionSelection(
                pending_question_type=PendingQuestionType.MISSING_INFO,
                pending_question_context={
                    QUESTION_CONTEXT_KEY: required_key,
                    QUESTION_CONTEXT_REQUIRED: True,
                    QUESTION_CONTEXT_PHASE: QUESTION_PHASE_PRE_GENERATION,
                    QUESTION_CONTEXT_REPROMPT_COUNT: 0,
                    QUESTION_CONTEXT_CLARIFICATION_COUNT: asked_clarifications + 1,
                },
                prompt_text=question_prompt(required_key, language),
                is_blocking_for_generation=True,
            )

    if not after_preview_generation:
        return None

    rules = REQUEST_TYPE_QUESTION_RULES.get(
        request_type,
        REQUEST_TYPE_QUESTION_RULES[AdRequestType.UNSET],
    )
    for optional_key in rules.optional_after_preview:
        if _is_missing(
            question_key=optional_key,
            has_product_name=has_product_name,
            has_price=has_price,
            has_store_type=has_store_type,
            has_creative_guidance=has_creative_guidance,
        ):
            return QuestionSelection(
                pending_question_type=PendingQuestionType.MISSING_INFO,
                pending_question_context={
                    QUESTION_CONTEXT_KEY: optional_key,
                    QUESTION_CONTEXT_REQUIRED: False,
                    QUESTION_CONTEXT_PHASE: QUESTION_PHASE_POST_PREVIEW,
                    QUESTION_CONTEXT_REPROMPT_COUNT: 0,
                },
                prompt_text=question_prompt(optional_key, language),
                is_blocking_for_generation=False,
            )

    if allow_regenerate_confirmation:
        return QuestionSelection(
            pending_question_type=PendingQuestionType.GENERATION_RETRY,
            pending_question_context={
                QUESTION_CONTEXT_KEY: QUESTION_KEY_REGENERATE_CONFIRMATION,
                QUESTION_CONTEXT_REQUIRED: False,
                QUESTION_CONTEXT_PHASE: QUESTION_PHASE_POST_PREVIEW,
                QUESTION_CONTEXT_REPROMPT_COUNT: 0,
            },
            prompt_text=regenerate_again_prompt(language),
            is_blocking_for_generation=False,
        )

    return None


def resolve_pending_question(
    *,
    pending_question_type: PendingQuestionType,
    pending_question_context: dict[str, Any] | None,
    status: PendingResolutionStatus,
    language: str,
    next_question: QuestionSelection | None = None,
    max_reprompts: int = MAX_REPROMPTS_DEFAULT,
) -> PendingQuestionTransition:
    context = dict(pending_question_context or {})
    question_key = pending_question_key(
        pending_question_type=pending_question_type,
        pending_question_context=context,
    )
    if status == PendingResolutionStatus.INTERRUPTED:
        return PendingQuestionTransition(
            status=status,
            pending_question_type=PendingQuestionType.NONE,
            pending_question_context={},
            reply_text=None,
        )
    if status == PendingResolutionStatus.RESOLVED:
        if next_question is None:
            return PendingQuestionTransition(
                status=status,
                pending_question_type=PendingQuestionType.NONE,
                pending_question_context={},
                reply_text=None,
            )
        return PendingQuestionTransition(
            status=status,
            pending_question_type=next_question.pending_question_type,
            pending_question_context=dict(next_question.pending_question_context),
            reply_text=next_question.prompt_text,
        )

    reprompt_count = _parse_reprompt_count(context.get(QUESTION_CONTEXT_REPROMPT_COUNT)) + 1
    if reprompt_count > max_reprompts:
        return PendingQuestionTransition(
            status=status,
            pending_question_type=PendingQuestionType.NONE,
            pending_question_context={},
            reply_text=reprompt_limit_message(language),
            exhausted_reprompts=True,
        )

    context[QUESTION_CONTEXT_REPROMPT_COUNT] = reprompt_count
    return PendingQuestionTransition(
        status=status,
        pending_question_type=pending_question_type,
        pending_question_context=context,
        reply_text=reprompt_prompt(question_key=question_key, language=language),
    )


def normalize_legacy_pending_followup(
    *,
    pending_question_type: PendingQuestionType,
    pending_question_context: dict[str, Any] | None,
    pending_followup_question: str | None,
) -> tuple[PendingQuestionType, dict[str, Any]]:
    context = dict(pending_question_context or {})
    if pending_question_type != PendingQuestionType.NONE:
        return pending_question_type, context

    if pending_followup_question is None:
        return pending_question_type, context

    if pending_followup_question == QUESTION_KEY_REGENERATE_CONFIRMATION:
        return (
            PendingQuestionType.GENERATION_RETRY,
            {
                QUESTION_CONTEXT_KEY: QUESTION_KEY_REGENERATE_CONFIRMATION,
                QUESTION_CONTEXT_REQUIRED: False,
                QUESTION_CONTEXT_PHASE: QUESTION_PHASE_POST_PREVIEW,
                QUESTION_CONTEXT_REPROMPT_COUNT: 0,
            },
        )

    if pending_followup_question in {
        QUESTION_KEY_PRICE,
        QUESTION_KEY_STORE_TYPE,
        QUESTION_KEY_CREATIVE_GUIDANCE,
    }:
        return (
            PendingQuestionType.MISSING_INFO,
            {
                QUESTION_CONTEXT_KEY: pending_followup_question,
                QUESTION_CONTEXT_REQUIRED: False,
                QUESTION_CONTEXT_PHASE: QUESTION_PHASE_POST_PREVIEW,
                QUESTION_CONTEXT_REPROMPT_COUNT: 0,
            },
        )

    return pending_question_type, context


def legacy_pending_followup_from_canonical(
    *,
    pending_question_type: PendingQuestionType,
    pending_question_context: dict[str, Any] | None,
) -> str | None:
    question_key = pending_question_key(
        pending_question_type=pending_question_type,
        pending_question_context=pending_question_context,
    )
    if question_key in {
        QUESTION_KEY_PRICE,
        QUESTION_KEY_STORE_TYPE,
        QUESTION_KEY_CREATIVE_GUIDANCE,
        QUESTION_KEY_REGENERATE_CONFIRMATION,
    }:
        return question_key
    return None


def classification_prompt(language: str) -> str:
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


def question_prompt(question_key: str, language: str) -> str:
    if question_key == QUESTION_KEY_PRODUCT_NAME:
        if language.lower() == "he":
            return "כדי ליצור מודעה אני צריך שם מוצר. מה שם המוצר?"
        return "To generate the ad, I need the product name. What is the product name?"
    if question_key == QUESTION_KEY_PRICE:
        if language.lower() == "he":
            return "מה המחיר המדויק שתרצה להציג במודעה?"
        return "What exact price should I display in the ad?"
    if question_key == QUESTION_KEY_STORE_TYPE:
        if language.lower() == "he":
            return "מה סוג העסק שלך? (למשל סופרמרקט, בגדים, תבלינים)"
        return "What is your store type? (for example grocery, clothing, spices)"
    if question_key == QUESTION_KEY_CREATIVE_GUIDANCE:
        if language.lower() == "he":
            return "יש לך הנחיות כלליות לסגנון המודעות? (צבעים, טון, אווירה)"
        return "Do you have general creative guidance? (colors, tone, style)"
    if question_key == QUESTION_KEY_REGENERATE_CONFIRMATION:
        return regenerate_again_prompt(language)
    return "Please provide the requested information."


def reprompt_prompt(*, question_key: str | None, language: str) -> str:
    if question_key == QUESTION_KEY_PRICE:
        if language.lower() == "he":
            return "לא הצלחתי לזהות מחיר. מה המחיר המדויק?"
        return "I could not detect a price. What is the exact price?"
    if language.lower() == "he":
        return "לא הצלחתי לזהות תשובה ברורה. " + question_prompt(
            question_key or "",
            language,
        )
    return "I could not detect a clear answer. " + question_prompt(question_key or "", language)


def reprompt_limit_message(language: str) -> str:
    if language.lower() == "he":
        return "לא חייבים לענות על זה עכשיו. אפשר להמשיך בלי המידע הזה כרגע."
    return "No problem, we can continue without this information for now."


def regenerate_again_prompt(language: str) -> str:
    if language.lower() == "he":
        return "כל השאלות הושלמו. רוצה שאפעיל עכשיו יצירת מודעה נוספת? (כן/לא)"
    return (
        "All follow-up questions are complete. Do you want me to generate another ad now? (yes/no)"
    )


def product_confirmation_use_buttons_prompt(language: str) -> str:
    if language.lower() == "he":
        return "כדי להמשיך, בחר באחד מכפתורי אישור המוצר."
    return "To continue, choose one of the product confirmation buttons."


def _parse_reprompt_count(value: Any) -> int:
    if isinstance(value, int):
        return max(value, 0)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return 0


def _required_before_generation_keys(*, request_type: AdRequestType) -> tuple[str, ...]:
    rules = REQUEST_TYPE_QUESTION_RULES.get(
        request_type,
        REQUEST_TYPE_QUESTION_RULES[AdRequestType.UNSET],
    )
    return tuple(
        key
        for key in rules.required_before_generation
        if key in _ALLOWED_MANDATORY_QUESTION_KEYS
    )


def _is_missing(
    *,
    question_key: str,
    has_product_name: bool,
    has_price: bool,
    has_store_type: bool,
    has_creative_guidance: bool,
) -> bool:
    if question_key == QUESTION_KEY_PRODUCT_NAME:
        return not has_product_name
    if question_key == QUESTION_KEY_PRICE:
        return not has_price
    if question_key == QUESTION_KEY_STORE_TYPE:
        return not has_store_type
    if question_key == QUESTION_KEY_CREATIVE_GUIDANCE:
        return not has_creative_guidance
    return False
