from adv_assistant import question_policy
from adv_assistant.db.enums import AdRequestType, PendingQuestionType
from adv_assistant.llm_gateway import Intent


def test_unresolved_classification_is_selected_first() -> None:
    selection = question_policy.select_next_question(
        request_type=AdRequestType.UNSET,
        classification_resolved=False,
        awaiting_product_confirmation=False,
        has_product_name=False,
        has_price=False,
        has_store_type=False,
        has_creative_guidance=False,
        language="he",
        after_preview_generation=False,
    )

    assert selection is not None
    assert selection.pending_question_type == PendingQuestionType.CLASSIFICATION
    assert "איזה סוג מודעה" in selection.prompt_text


def test_product_confirmation_overrides_optional_followup() -> None:
    selection = question_policy.select_next_question(
        request_type=AdRequestType.SINGLE_PRODUCT,
        classification_resolved=True,
        awaiting_product_confirmation=True,
        has_product_name=True,
        has_price=False,
        has_store_type=False,
        has_creative_guidance=False,
        language="he",
        after_preview_generation=True,
    )

    assert selection is not None
    assert selection.pending_question_type == PendingQuestionType.PRODUCT_CONFIRMATION


def test_required_question_is_selected_before_optional() -> None:
    selection = question_policy.select_next_question(
        request_type=AdRequestType.SINGLE_PRODUCT,
        classification_resolved=True,
        awaiting_product_confirmation=False,
        has_product_name=False,
        has_price=False,
        has_store_type=False,
        has_creative_guidance=False,
        language="he",
        after_preview_generation=True,
    )

    assert selection is not None
    assert selection.pending_question_type == PendingQuestionType.MISSING_INFO
    assert selection.pending_question_context[question_policy.QUESTION_CONTEXT_KEY] == (
        question_policy.QUESTION_KEY_PRODUCT_NAME
    )
    assert selection.pending_question_context[question_policy.QUESTION_CONTEXT_REQUIRED] is True
    assert (
        selection.pending_question_context[question_policy.QUESTION_CONTEXT_CLARIFICATION_COUNT]
        == 1
    )
    assert selection.is_blocking_for_generation is True


def test_optional_question_not_asked_before_preview_when_generation_can_proceed() -> None:
    selection = question_policy.select_next_question(
        request_type=AdRequestType.SINGLE_PRODUCT,
        classification_resolved=True,
        awaiting_product_confirmation=False,
        has_product_name=True,
        has_price=False,
        has_store_type=False,
        has_creative_guidance=False,
        language="he",
        after_preview_generation=False,
    )

    assert selection is not None
    assert selection.pending_question_type == PendingQuestionType.MISSING_INFO
    assert selection.pending_question_context[question_policy.QUESTION_CONTEXT_KEY] == (
        question_policy.QUESTION_KEY_PRICE
    )
    assert selection.pending_question_context[question_policy.QUESTION_CONTEXT_REQUIRED] is True
    assert selection.is_blocking_for_generation is True


def test_optional_followup_is_selected_after_preview_only() -> None:
    selection = question_policy.select_next_question(
        request_type=AdRequestType.SINGLE_PRODUCT,
        classification_resolved=True,
        awaiting_product_confirmation=False,
        has_product_name=True,
        has_price=True,
        has_store_type=False,
        has_creative_guidance=False,
        language="he",
        after_preview_generation=True,
    )

    assert selection is not None
    assert selection.pending_question_type == PendingQuestionType.MISSING_INFO
    assert selection.pending_question_context[question_policy.QUESTION_CONTEXT_KEY] == (
        question_policy.QUESTION_KEY_STORE_TYPE
    )
    assert selection.pending_question_context[question_policy.QUESTION_CONTEXT_REQUIRED] is False
    assert selection.is_blocking_for_generation is False


def test_ambiguous_answer_reprompts_same_key() -> None:
    transition = question_policy.resolve_pending_question(
        pending_question_type=PendingQuestionType.MISSING_INFO,
        pending_question_context={
            question_policy.QUESTION_CONTEXT_KEY: question_policy.QUESTION_KEY_PRICE,
            question_policy.QUESTION_CONTEXT_REPROMPT_COUNT: 0,
        },
        status=question_policy.PendingResolutionStatus.UNRESOLVED,
        language="he",
    )

    assert transition.pending_question_type == PendingQuestionType.MISSING_INFO
    assert transition.pending_question_context[question_policy.QUESTION_CONTEXT_KEY] == (
        question_policy.QUESTION_KEY_PRICE
    )
    assert transition.pending_question_context[question_policy.QUESTION_CONTEXT_REPROMPT_COUNT] == 1
    assert transition.reply_text is not None
    assert "לא הצלחתי לזהות מחיר" in transition.reply_text


def test_higher_priority_interrupt_clears_pending_question() -> None:
    assert Intent.PUBLISH_AD in question_policy.INTERRUPT_PENDING_QUESTION_INTENTS

    transition = question_policy.resolve_pending_question(
        pending_question_type=PendingQuestionType.MISSING_INFO,
        pending_question_context={
            question_policy.QUESTION_CONTEXT_KEY: question_policy.QUESTION_KEY_STORE_TYPE,
            question_policy.QUESTION_CONTEXT_REPROMPT_COUNT: 1,
        },
        status=question_policy.PendingResolutionStatus.INTERRUPTED,
        language="he",
    )

    assert transition.pending_question_type == PendingQuestionType.NONE
    assert transition.pending_question_context == {}
    assert transition.reply_text is None


def test_reprompt_limit_safeguard_clears_pending_question() -> None:
    transition = question_policy.resolve_pending_question(
        pending_question_type=PendingQuestionType.MISSING_INFO,
        pending_question_context={
            question_policy.QUESTION_CONTEXT_KEY: question_policy.QUESTION_KEY_STORE_TYPE,
            question_policy.QUESTION_CONTEXT_REPROMPT_COUNT: 2,
        },
        status=question_policy.PendingResolutionStatus.UNRESOLVED,
        language="he",
        max_reprompts=2,
    )

    assert transition.pending_question_type == PendingQuestionType.NONE
    assert transition.pending_question_context == {}
    assert transition.exhausted_reprompts is True
    assert transition.reply_text is not None
    assert "אפשר להמשיך" in transition.reply_text


def test_pre_generation_clarification_budget_blocks_fourth_question(monkeypatch) -> None:
    monkeypatch.setitem(
        question_policy.REQUEST_TYPE_QUESTION_RULES,
        AdRequestType.SINGLE_PRODUCT,
        question_policy.RequestTypeQuestionRules(
            required_before_generation=(
                question_policy.QUESTION_KEY_PRODUCT_NAME,
                question_policy.QUESTION_KEY_PRICE,
                question_policy.QUESTION_KEY_STORE_TYPE,
                question_policy.QUESTION_KEY_CREATIVE_GUIDANCE,
            ),
            optional_after_preview=(),
        ),
    )

    selection = question_policy.select_next_question(
        request_type=AdRequestType.SINGLE_PRODUCT,
        classification_resolved=True,
        awaiting_product_confirmation=False,
        has_product_name=True,
        has_price=True,
        has_store_type=True,
        has_creative_guidance=False,
        language="he",
        after_preview_generation=False,
        clarification_question_count=3,
    )

    assert selection is None


def test_generation_ready_when_budget_reached_even_if_required_field_missing(monkeypatch) -> None:
    monkeypatch.setitem(
        question_policy.REQUEST_TYPE_QUESTION_RULES,
        AdRequestType.SINGLE_PRODUCT,
        question_policy.RequestTypeQuestionRules(
            required_before_generation=(
                question_policy.QUESTION_KEY_PRODUCT_NAME,
                question_policy.QUESTION_KEY_PRICE,
                question_policy.QUESTION_KEY_STORE_TYPE,
                question_policy.QUESTION_KEY_CREATIVE_GUIDANCE,
            ),
            optional_after_preview=(),
        ),
    )

    ready = question_policy.is_generation_ready(
        request_type=AdRequestType.SINGLE_PRODUCT,
        classification_resolved=True,
        awaiting_product_confirmation=False,
        has_product_name=True,
        has_price=True,
        has_store_type=True,
        has_creative_guidance=False,
        clarification_question_count=3,
    )

    assert ready is True


def test_missing_mandatory_fields_returns_required_keys_only() -> None:
    missing = question_policy.missing_mandatory_fields(
        request_type=AdRequestType.SINGLE_PRODUCT,
        has_product_name=False,
        has_price=False,
        has_store_type=False,
        has_creative_guidance=False,
    )

    assert missing == [
        question_policy.QUESTION_KEY_PRODUCT_NAME,
        question_policy.QUESTION_KEY_PRICE,
    ]


def test_missing_mandatory_fields_empty_when_all_required_present() -> None:
    missing = question_policy.missing_mandatory_fields(
        request_type=AdRequestType.SINGLE_PRODUCT,
        has_product_name=True,
        has_price=True,
        has_store_type=False,
        has_creative_guidance=False,
    )

    assert missing == []


def test_missing_mandatory_fields_ignores_unknown_required_keys(monkeypatch) -> None:
    monkeypatch.setitem(
        question_policy.REQUEST_TYPE_QUESTION_RULES,
        AdRequestType.SINGLE_PRODUCT,
        question_policy.RequestTypeQuestionRules(
            required_before_generation=(
                question_policy.QUESTION_KEY_PRODUCT_NAME,
                "expiration_date",
                question_policy.QUESTION_KEY_PRICE,
            ),
            optional_after_preview=(),
        ),
    )
    missing = question_policy.missing_mandatory_fields(
        request_type=AdRequestType.SINGLE_PRODUCT,
        has_product_name=False,
        has_price=False,
        has_store_type=False,
        has_creative_guidance=False,
    )

    assert missing == [
        question_policy.QUESTION_KEY_PRODUCT_NAME,
        question_policy.QUESTION_KEY_PRICE,
    ]
