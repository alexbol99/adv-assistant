from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

STAGE_NAME = "creative_brief"
SESSION_CONTEXT_KEY = "creative_brief_session"
DEFAULT_SOFT_QUESTION_CAP = 4


class CreativeBriefDecision(StrEnum):
    ASK_QUESTION = "ask_question"
    BRIEF_READY = "brief_ready"


def _normalize_text(value: Any, *, max_length: int = 500) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    normalized = " ".join(value.split()).strip()
    if not normalized:
        return None
    return normalized[:max_length]


def _normalize_string_list(values: Any, *, max_items: int = 20) -> list[str]:
    if not isinstance(values, list):
        return []
    result: list[str] = []
    for value in values[:max_items]:
        normalized = _normalize_text(value, max_length=120)
        if normalized is not None:
            result.append(normalized)
    return result


class NextQuestion(BaseModel):
    key: str = Field(min_length=1, max_length=80)
    question_text: str = Field(min_length=1, max_length=240)

    @field_validator("key", mode="before")
    @classmethod
    def _normalize_key(cls, value: Any) -> str:
        normalized = _normalize_text(value, max_length=80)
        return normalized or "creative_direction"

    @field_validator("question_text", mode="before")
    @classmethod
    def _normalize_question_text(cls, value: Any) -> str:
        normalized = _normalize_text(value, max_length=240)
        return normalized or "What visual direction do you want for this ad?"


class CurrentBriefState(BaseModel):
    goal: str | None = Field(default=None, max_length=200)
    scene: str | None = Field(default=None, max_length=300)
    style: str | None = Field(default=None, max_length=300)
    audience: str | None = Field(default=None, max_length=200)
    platform: str | None = Field(default=None, max_length=120)
    format: str | None = Field(default=None, max_length=80)
    text_overlay: str | None = Field(default=None, max_length=300)
    realism: str | None = Field(default=None, max_length=120)
    marketing_angle: str | None = Field(default=None, max_length=240)
    value_proposition: str | None = Field(default=None, max_length=240)
    constraints: list[str] = Field(default_factory=list)
    forbidden_elements: list[str] = Field(default_factory=list)

    @field_validator(
        "goal",
        "scene",
        "style",
        "audience",
        "platform",
        "format",
        "text_overlay",
        "realism",
        "marketing_angle",
        "value_proposition",
        mode="before",
    )
    @classmethod
    def _normalize_field_text(cls, value: Any) -> str | None:
        return _normalize_text(value, max_length=300)

    @field_validator("constraints", "forbidden_elements", mode="before")
    @classmethod
    def _normalize_lists(cls, value: Any) -> list[str]:
        return _normalize_string_list(value)


class FinalCreativeBrief(CurrentBriefState):
    product_identity: str | None = Field(default=None, max_length=240)
    summary: str | None = Field(default=None, max_length=500)
    must_include: list[str] = Field(default_factory=list)
    inferred_defaults: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("product_identity", "summary", mode="before")
    @classmethod
    def _normalize_summary_text(cls, value: Any) -> str | None:
        return _normalize_text(value, max_length=500)

    @field_validator("must_include", "inferred_defaults", mode="before")
    @classmethod
    def _normalize_summary_lists(cls, value: Any) -> list[str]:
        return _normalize_string_list(value)


class CollectedAnswer(BaseModel):
    question_key: str | None = Field(default=None, max_length=80)
    answer_text: str = Field(min_length=1, max_length=500)

    @field_validator("question_key", mode="before")
    @classmethod
    def _normalize_answer_key(cls, value: Any) -> str | None:
        return _normalize_text(value, max_length=80)

    @field_validator("answer_text", mode="before")
    @classmethod
    def _normalize_answer_text(cls, value: Any) -> str:
        normalized = _normalize_text(value, max_length=500)
        return normalized or "n/a"


class CreativeBriefSessionState(BaseModel):
    confirmed_product: dict[str, Any] = Field(default_factory=dict)
    user_memory_context: dict[str, Any] = Field(default_factory=dict)
    conversation_context: list[str] = Field(default_factory=list)
    collected_answers: list[CollectedAnswer] = Field(default_factory=list)
    inferred_brief_fields: CurrentBriefState = Field(default_factory=CurrentBriefState)
    missing_dimensions: list[str] = Field(default_factory=list)
    pending_question: NextQuestion | None = None
    is_brief_ready: bool = False
    final_brief: FinalCreativeBrief | None = None
    question_count: int = 0
    asked_question_keys: list[str] = Field(default_factory=list)
    source_intent: str | None = Field(default=None, max_length=64)

    @field_validator("conversation_context", mode="before")
    @classmethod
    def _normalize_conversation_context(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        normalized: list[str] = []
        for item in value[:6]:
            txt = _normalize_text(item, max_length=180)
            if txt is not None:
                normalized.append(txt)
        return normalized

    @field_validator("missing_dimensions", "asked_question_keys", mode="before")
    @classmethod
    def _normalize_dimension_list(cls, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        seen: set[str] = set()
        result: list[str] = []
        for item in value[:20]:
            normalized = _normalize_text(item, max_length=80)
            if normalized is None:
                continue
            if normalized in seen:
                continue
            result.append(normalized)
            seen.add(normalized)
        return result

    @field_validator("source_intent", mode="before")
    @classmethod
    def _normalize_source_intent(cls, value: Any) -> str | None:
        return _normalize_text(value, max_length=64)


class CreativeBriefPlannerContext(BaseModel):
    language: str = Field(default="en", max_length=8)
    source_intent: str | None = Field(default=None, max_length=64)
    latest_user_message: str | None = Field(default=None, max_length=500)
    session_state: CreativeBriefSessionState

    @field_validator("language", mode="before")
    @classmethod
    def _normalize_language(cls, value: Any) -> str:
        normalized = _normalize_text(value, max_length=8)
        return normalized or "en"

    @field_validator("latest_user_message", mode="before")
    @classmethod
    def _normalize_latest_user_message(cls, value: Any) -> str | None:
        return _normalize_text(value, max_length=500)

    @field_validator("source_intent", mode="before")
    @classmethod
    def _normalize_planner_intent(cls, value: Any) -> str | None:
        return _normalize_text(value, max_length=64)


class CreativeBriefPlannerOutput(BaseModel):
    decision: CreativeBriefDecision
    next_question: NextQuestion | None = None
    current_brief_state: CurrentBriefState = Field(default_factory=CurrentBriefState)
    missing_dimensions: list[str] = Field(default_factory=list)
    final_brief: FinalCreativeBrief | None = None
    internal_notes: str | None = Field(default=None, max_length=600)
    confidence: float | None = Field(default=None, ge=0, le=1)

    @field_validator("missing_dimensions", mode="before")
    @classmethod
    def _normalize_missing_dimensions(cls, value: Any) -> list[str]:
        return _normalize_string_list(value)

    @field_validator("internal_notes", mode="before")
    @classmethod
    def _normalize_internal_notes(cls, value: Any) -> str | None:
        return _normalize_text(value, max_length=600)


_ASK_DECISION_ALIASES = {
    "ask",
    "ask_question",
    "question",
    "need_more_info",
    "need_more_information",
    "more_info",
    "clarify",
    "clarification",
    "missing_info",
    "missing_information",
    "not_ready",
}
_READY_DECISION_ALIASES = {
    "ready",
    "brief_ready",
    "done",
    "complete",
    "completed",
    "sufficient",
    "enough_info",
}


def _coerce_decision_value(value: Any) -> str:
    normalized = _normalize_text(value, max_length=60)
    if normalized is None:
        return CreativeBriefDecision.ASK_QUESTION.value
    key = normalized.lower().replace("-", "_").replace(" ", "_")
    if key in _ASK_DECISION_ALIASES:
        return CreativeBriefDecision.ASK_QUESTION.value
    if key in _READY_DECISION_ALIASES:
        return CreativeBriefDecision.BRIEF_READY.value
    if "need" in key and "info" in key:
        return CreativeBriefDecision.ASK_QUESTION.value
    if "ready" in key or "complete" in key or "done" in key:
        return CreativeBriefDecision.BRIEF_READY.value
    return CreativeBriefDecision.ASK_QUESTION.value


def _coerce_next_question_payload(payload: dict[str, Any]) -> dict[str, str] | None:
    raw_question = payload.get("next_question")
    if isinstance(raw_question, dict):
        question_text = (
            raw_question.get("question_text")
            or raw_question.get("question")
            or raw_question.get("text")
            or payload.get("question_text")
            or payload.get("question")
        )
        key = (
            raw_question.get("key")
            or raw_question.get("question_key")
            or payload.get("next_question_key")
            or payload.get("question_key")
            or "creative_direction"
        )
        if _normalize_text(question_text, max_length=240) is None:
            return None
        return {
            "key": key,
            "question_text": question_text,
        }
    if isinstance(raw_question, str):
        return {
            "key": payload.get("next_question_key") or payload.get("question_key") or "creative_direction",
            "question_text": raw_question,
        }
    top_level_question = payload.get("question_text") or payload.get("question")
    if isinstance(top_level_question, str):
        return {
            "key": payload.get("next_question_key") or payload.get("question_key") or "creative_direction",
            "question_text": top_level_question,
        }
    return None


def coerce_planner_output_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        text = _normalize_text(payload, max_length=240)
        if text is None:
            return {"decision": CreativeBriefDecision.ASK_QUESTION.value}
        return {
            "decision": CreativeBriefDecision.ASK_QUESTION.value,
            "next_question": {"key": "creative_direction", "question_text": text},
        }

    normalized: dict[str, Any] = dict(payload)
    normalized["decision"] = _coerce_decision_value(payload.get("decision"))

    next_question = _coerce_next_question_payload(payload)
    if next_question is not None:
        normalized["next_question"] = next_question

    if not isinstance(normalized.get("current_brief_state"), dict):
        for candidate in ("brief_state", "current_state", "brief"):
            value = payload.get(candidate)
            if isinstance(value, dict):
                normalized["current_brief_state"] = value
                break

    if not isinstance(normalized.get("missing_dimensions"), list):
        for candidate in ("missing", "missing_fields", "gaps"):
            value = payload.get(candidate)
            if isinstance(value, list):
                normalized["missing_dimensions"] = value
                break

    if not isinstance(normalized.get("final_brief"), dict):
        for candidate in ("final", "brief_ready_payload"):
            value = payload.get(candidate)
            if isinstance(value, dict):
                normalized["final_brief"] = value
                break

    return normalized


@dataclass(slots=True)
class PlannerResolution:
    state: CreativeBriefSessionState
    forced_reason: str | None = None
    validation_fallback: bool = False


def initialize_session_state(
    *,
    confirmed_product: dict[str, Any],
    user_memory_context: dict[str, Any],
    conversation_context: list[str],
    source_intent: str | None,
) -> CreativeBriefSessionState:
    inferred = CurrentBriefState(
        style=_normalize_text(user_memory_context.get("creative_guidance"), max_length=300),
        audience=_normalize_text(user_memory_context.get("store_type"), max_length=200),
    )
    return CreativeBriefSessionState(
        confirmed_product=confirmed_product,
        user_memory_context=user_memory_context,
        conversation_context=conversation_context,
        inferred_brief_fields=inferred,
        source_intent=_normalize_text(source_intent, max_length=64),
    )


def _has_product_identity(state: CreativeBriefSessionState) -> bool:
    confirmed = state.confirmed_product
    product_name = _normalize_text(confirmed.get("product_name"), max_length=240)
    retailer_title = _normalize_text(confirmed.get("retailer_title"), max_length=240)
    brand = _normalize_text(confirmed.get("brand"), max_length=240)
    return product_name is not None or retailer_title is not None or brand is not None


def _has_scene_or_intent(state: CreativeBriefSessionState) -> bool:
    fields = state.inferred_brief_fields
    return any(
        _normalize_text(value, max_length=300) is not None
        for value in (fields.goal, fields.scene, fields.marketing_angle, fields.value_proposition)
    )


def _has_style_context(state: CreativeBriefSessionState) -> bool:
    fields = state.inferred_brief_fields
    memory_style = _normalize_text(
        state.user_memory_context.get("creative_guidance"),
        max_length=300,
    )
    return any(
        _normalize_text(value, max_length=300) is not None
        for value in (fields.style, fields.audience, fields.platform, fields.format, memory_style)
    )


def compute_missing_dimensions(state: CreativeBriefSessionState) -> list[str]:
    missing: list[str] = []
    if not _has_product_identity(state):
        missing.append("product_identity")
    if not _has_scene_or_intent(state):
        missing.append("creative_direction")
    if not _has_style_context(state):
        missing.append("style_context")
    return missing


def is_brief_sufficient(state: CreativeBriefSessionState) -> bool:
    return not compute_missing_dimensions(state)


def _merge_brief_state(
    *,
    base: CurrentBriefState,
    incoming: CurrentBriefState,
) -> CurrentBriefState:
    updates: dict[str, Any] = {}
    for key in (
        "goal",
        "scene",
        "style",
        "audience",
        "platform",
        "format",
        "text_overlay",
        "realism",
        "marketing_angle",
        "value_proposition",
    ):
        value = getattr(incoming, key)
        if value is not None:
            updates[key] = value

    constraints = list(base.constraints)
    for value in incoming.constraints:
        if value not in constraints:
            constraints.append(value)
    forbidden = list(base.forbidden_elements)
    for value in incoming.forbidden_elements:
        if value not in forbidden:
            forbidden.append(value)
    updates["constraints"] = constraints
    updates["forbidden_elements"] = forbidden
    return base.model_copy(update=updates)


def _build_product_identity_text(state: CreativeBriefSessionState) -> str | None:
    confirmed = state.confirmed_product
    product_name = _normalize_text(confirmed.get("product_name"), max_length=240)
    brand = _normalize_text(confirmed.get("brand"), max_length=120)
    retailer_title = _normalize_text(confirmed.get("retailer_title"), max_length=240)
    if product_name and brand:
        return f"{brand} {product_name}"
    return product_name or retailer_title or brand


def synthesize_final_brief(
    state: CreativeBriefSessionState,
    *,
    confidence: float | None,
) -> FinalCreativeBrief:
    fields = state.inferred_brief_fields
    defaults: list[str] = []
    if fields.format is None:
        defaults.append("format:16:9")
    if fields.platform is None:
        defaults.append("platform:whatsapp")
    if fields.realism is None:
        defaults.append("realism:photo-real")

    must_include = _normalize_string_list(
        [
            _build_product_identity_text(state),
            fields.text_overlay,
            state.user_memory_context.get("business_name"),
        ]
    )
    summary_parts = [
        _build_product_identity_text(state),
        fields.scene,
        fields.style or state.user_memory_context.get("creative_guidance"),
        fields.marketing_angle,
    ]
    summary = _normalize_text(" | ".join(p for p in summary_parts if p), max_length=500)
    return FinalCreativeBrief(
        product_identity=_build_product_identity_text(state),
        summary=summary,
        goal=fields.goal,
        scene=fields.scene,
        style=fields.style or _normalize_text(state.user_memory_context.get("creative_guidance")),
        audience=fields.audience,
        platform=fields.platform,
        format=fields.format,
        text_overlay=fields.text_overlay,
        realism=fields.realism,
        marketing_angle=fields.marketing_angle,
        value_proposition=fields.value_proposition,
        constraints=fields.constraints,
        forbidden_elements=fields.forbidden_elements,
        must_include=must_include,
        inferred_defaults=defaults,
        confidence=confidence,
    )


def _default_question_for_dimension(dimension: str, language: str) -> NextQuestion:
    language_code = (language or "en").lower()
    he = language_code == "he"
    if dimension == "product_identity":
        return NextQuestion(
            key="product_identity",
            question_text=(
                "איזה מוצר מדויק תרצה להציג במודעה?"
                if he
                else "Which exact product should be featured in the ad?"
            ),
        )
    if dimension == "style_context":
        return NextQuestion(
            key="style",
            question_text=(
                "איזה סגנון חזותי תרצה? למשל נקי, יוקרתי, צבעוני או דרמטי."
                if he
                else "What visual style do you want (for example clean, premium, colorful, dramatic)?"
            ),
        )
    return NextQuestion(
        key="creative_direction",
        question_text=(
            "מה הכיוון היצירתי המרכזי למודעה? למשל אווירה, סצנה או מסר שיווקי."
            if he
            else "What is the main creative direction (scene, mood, or marketing angle)?"
        ),
    )


def apply_planner_output(
    *,
    session_state: CreativeBriefSessionState,
    planner_output: CreativeBriefPlannerOutput,
    latest_user_message: str | None,
    language: str,
    question_cap: int = DEFAULT_SOFT_QUESTION_CAP,
) -> PlannerResolution:
    state = session_state.model_copy(deep=True)
    state.inferred_brief_fields = _merge_brief_state(
        base=state.inferred_brief_fields,
        incoming=planner_output.current_brief_state,
    )

    if latest_user_message is not None and state.pending_question is not None:
        state.collected_answers.append(
            CollectedAnswer(
                question_key=state.pending_question.key,
                answer_text=latest_user_message,
            )
        )

    provided_missing = []
    for dimension in planner_output.missing_dimensions:
        normalized = _normalize_text(dimension, max_length=80)
        if normalized is not None and normalized not in provided_missing:
            provided_missing.append(normalized)
    state.missing_dimensions = provided_missing or compute_missing_dimensions(state)

    if planner_output.decision == CreativeBriefDecision.ASK_QUESTION:
        if state.question_count >= question_cap:
            state.pending_question = None
            state.is_brief_ready = True
            state.final_brief = synthesize_final_brief(state, confidence=0.4)
            state.missing_dimensions = compute_missing_dimensions(state)
            return PlannerResolution(state=state, forced_reason="cap")

        if is_brief_sufficient(state):
            state.pending_question = None
            state.is_brief_ready = True
            state.final_brief = synthesize_final_brief(
                state,
                confidence=planner_output.confidence or 0.7,
            )
            state.missing_dimensions = []
            return PlannerResolution(state=state, forced_reason="already_sufficient")

        question = planner_output.next_question
        if question is None:
            question = _default_question_for_dimension(state.missing_dimensions[0], language)
        known_key = question.key in state.asked_question_keys
        if known_key:
            state.pending_question = None
            state.is_brief_ready = True
            state.final_brief = synthesize_final_brief(state, confidence=0.45)
            state.missing_dimensions = compute_missing_dimensions(state)
            return PlannerResolution(
                state=state,
                forced_reason="validation_fallback",
                validation_fallback=True,
            )

        state.pending_question = question
        state.is_brief_ready = False
        state.final_brief = None
        state.question_count = max(0, state.question_count) + 1
        if question.key not in state.asked_question_keys:
            state.asked_question_keys.append(question.key)
        return PlannerResolution(state=state)

    if planner_output.final_brief is not None:
        final_brief = planner_output.final_brief
    else:
        final_brief = synthesize_final_brief(
            state,
            confidence=planner_output.confidence or 0.7,
        )

    state.pending_question = None
    state.is_brief_ready = True
    state.final_brief = final_brief
    state.missing_dimensions = compute_missing_dimensions(state)
    if not is_brief_sufficient(state) and state.question_count < question_cap:
        fallback_question = _default_question_for_dimension(state.missing_dimensions[0], language)
        state.pending_question = fallback_question
        state.is_brief_ready = False
        state.final_brief = None
        state.question_count = max(0, state.question_count) + 1
        if fallback_question.key not in state.asked_question_keys:
            state.asked_question_keys.append(fallback_question.key)
        return PlannerResolution(
            state=state,
            forced_reason="validation_fallback",
            validation_fallback=True,
        )
    return PlannerResolution(state=state)


def render_brief_instruction(final_brief: FinalCreativeBrief) -> str:
    lines: list[str] = ["Use this creative brief for image generation:"]
    if final_brief.product_identity:
        lines.append(f"- Product identity: {final_brief.product_identity}")
    if final_brief.goal:
        lines.append(f"- Goal: {final_brief.goal}")
    if final_brief.scene:
        lines.append(f"- Scene: {final_brief.scene}")
    if final_brief.style:
        lines.append(f"- Style: {final_brief.style}")
    if final_brief.audience:
        lines.append(f"- Audience: {final_brief.audience}")
    if final_brief.platform:
        lines.append(f"- Platform/use case: {final_brief.platform}")
    if final_brief.format:
        lines.append(f"- Format/aspect: {final_brief.format}")
    if final_brief.marketing_angle:
        lines.append(f"- Marketing angle: {final_brief.marketing_angle}")
    if final_brief.value_proposition:
        lines.append(f"- Value proposition: {final_brief.value_proposition}")
    if final_brief.text_overlay:
        lines.append(f"- Text overlay: {final_brief.text_overlay}")
    if final_brief.realism:
        lines.append(f"- Realism: {final_brief.realism}")
    if final_brief.constraints:
        lines.append(f"- Constraints: {', '.join(final_brief.constraints)}")
    if final_brief.forbidden_elements:
        lines.append(f"- Forbidden elements: {', '.join(final_brief.forbidden_elements)}")
    return "\n".join(lines)


def noop_plan_creative_brief(
    *,
    context: CreativeBriefPlannerContext,
) -> CreativeBriefPlannerOutput:
    state = context.session_state
    missing = compute_missing_dimensions(state)
    if not missing:
        return CreativeBriefPlannerOutput(
            decision=CreativeBriefDecision.BRIEF_READY,
            current_brief_state=state.inferred_brief_fields,
            missing_dimensions=[],
            final_brief=synthesize_final_brief(state, confidence=0.6),
            internal_notes="noop_planner_brief_ready",
            confidence=0.6,
        )
    return CreativeBriefPlannerOutput(
        decision=CreativeBriefDecision.ASK_QUESTION,
        next_question=_default_question_for_dimension(missing[0], context.language),
        current_brief_state=state.inferred_brief_fields,
        missing_dimensions=missing,
        final_brief=None,
        internal_notes="noop_planner_question",
        confidence=0.5,
    )
