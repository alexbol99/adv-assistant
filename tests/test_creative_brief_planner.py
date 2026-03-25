from adv_assistant import creative_brief_planner


def _base_state(
    *,
    creative_guidance: str | None = None,
    store_type: str | None = None,
) -> creative_brief_planner.CreativeBriefSessionState:
    return creative_brief_planner.initialize_session_state(
        confirmed_product={
            "product_name": "Magnum Ice Cream",
            "brand": "Magnum",
            "category": "Ice Cream",
        },
        user_memory_context={
            "business_name": "My Store",
            "store_type": store_type,
            "creative_guidance": creative_guidance,
        },
        conversation_context=["user: create ad for Magnum"],
        source_intent="create_ad",
    )


def test_no_question_when_context_already_sufficient() -> None:
    state = _base_state(creative_guidance="clean premium look")
    state.inferred_brief_fields.goal = "Highlight premium quality"
    state.inferred_brief_fields.scene = "Product hero shot on marble table"
    output = creative_brief_planner.noop_plan_creative_brief(
        context=creative_brief_planner.CreativeBriefPlannerContext(
            language="en",
            source_intent="create_ad",
            latest_user_message=None,
            session_state=state,
        )
    )

    assert output.decision == creative_brief_planner.CreativeBriefDecision.BRIEF_READY
    assert output.next_question is None
    assert output.final_brief is not None


def test_missing_critical_info_asks_one_high_value_question() -> None:
    state = _base_state(creative_guidance=None)
    output = creative_brief_planner.noop_plan_creative_brief(
        context=creative_brief_planner.CreativeBriefPlannerContext(
            language="en",
            source_intent="create_ad",
            latest_user_message=None,
            session_state=state,
        )
    )

    assert output.decision == creative_brief_planner.CreativeBriefDecision.ASK_QUESTION
    assert output.next_question is not None
    assert output.next_question.key in {"creative_direction", "style"}
    assert isinstance(output.next_question.question_text, str)


def test_answer_can_transition_to_brief_ready() -> None:
    state = _base_state(creative_guidance="bright modern style")
    state.pending_question = creative_brief_planner.NextQuestion(
        key="creative_direction",
        question_text="What is the main creative direction?",
    )
    ask_output = creative_brief_planner.CreativeBriefPlannerOutput(
        decision=creative_brief_planner.CreativeBriefDecision.BRIEF_READY,
        current_brief_state=creative_brief_planner.CurrentBriefState(
            goal="Boost impulse purchase",
            scene="Close-up hero shot with cold mist",
        ),
        final_brief=creative_brief_planner.FinalCreativeBrief(
            product_identity="Magnum Ice Cream",
            goal="Boost impulse purchase",
            scene="Close-up hero shot with cold mist",
            style="bright modern style",
            summary="Magnum product hero, cold and premium",
            confidence=0.88,
        ),
        confidence=0.88,
    )
    resolution = creative_brief_planner.apply_planner_output(
        session_state=state,
        planner_output=ask_output,
        latest_user_message="Product hero in a summer beach setting",
        language="en",
        question_cap=4,
    )

    assert resolution.state.is_brief_ready is True
    assert resolution.state.final_brief is not None
    assert resolution.state.collected_answers
    assert resolution.state.collected_answers[-1].question_key == "creative_direction"


def test_user_memory_reused_instead_of_reasking_style() -> None:
    state = _base_state(
        creative_guidance="minimal clean typography",
        store_type="Neighborhood grocery",
    )
    missing = creative_brief_planner.compute_missing_dimensions(state)

    assert "style_context" not in missing


def test_only_one_question_is_tracked_per_turn() -> None:
    state = _base_state(creative_guidance=None)
    output = creative_brief_planner.CreativeBriefPlannerOutput(
        decision=creative_brief_planner.CreativeBriefDecision.ASK_QUESTION,
        next_question=creative_brief_planner.NextQuestion(
            key="style",
            question_text="What style should the ad use?",
        ),
        current_brief_state=creative_brief_planner.CurrentBriefState(
            goal="Drive store visits",
            scene="Product on clean background",
        ),
        missing_dimensions=["style_context"],
        confidence=0.7,
    )
    resolution = creative_brief_planner.apply_planner_output(
        session_state=state,
        planner_output=output,
        latest_user_message=None,
        language="en",
        question_cap=4,
    )

    assert resolution.state.pending_question is not None
    assert resolution.state.pending_question.key == "style"
    assert resolution.state.question_count == 1


def test_soft_cap_forces_ready_with_defaults() -> None:
    state = _base_state(creative_guidance=None)
    state.question_count = 4
    output = creative_brief_planner.CreativeBriefPlannerOutput(
        decision=creative_brief_planner.CreativeBriefDecision.ASK_QUESTION,
        next_question=creative_brief_planner.NextQuestion(
            key="creative_direction",
            question_text="What scene do you want?",
        ),
        missing_dimensions=["creative_direction", "style_context"],
        confidence=0.4,
    )
    resolution = creative_brief_planner.apply_planner_output(
        session_state=state,
        planner_output=output,
        latest_user_message=None,
        language="en",
        question_cap=4,
    )

    assert resolution.state.is_brief_ready is True
    assert resolution.state.final_brief is not None
    assert resolution.forced_reason == "cap"


def test_fallback_uses_pending_creative_direction_answer() -> None:
    state = _base_state(
        creative_guidance="clean premium style",
        store_type="Neighborhood grocery",
    )
    state.pending_question = creative_brief_planner.NextQuestion(
        key="creative_direction",
        question_text="What is the main creative direction?",
    )
    state.asked_question_keys = ["creative_direction"]
    state.question_count = 1

    degraded_output = creative_brief_planner.CreativeBriefPlannerOutput(
        decision=creative_brief_planner.CreativeBriefDecision.ASK_QUESTION,
        next_question=creative_brief_planner.NextQuestion(
            key="creative_direction",
            question_text="What is the main creative direction?",
        ),
        current_brief_state=creative_brief_planner.CurrentBriefState(),
        missing_dimensions=["creative_direction"],
        confidence=0.4,
    )

    resolution = creative_brief_planner.apply_planner_output(
        session_state=state,
        planner_output=degraded_output,
        latest_user_message="Strong marketing message about value",
        language="en",
        question_cap=4,
    )

    assert resolution.state.is_brief_ready is True
    assert resolution.forced_reason == "already_sufficient"
    assert resolution.state.pending_question is None
    assert resolution.state.inferred_brief_fields.marketing_angle is not None
