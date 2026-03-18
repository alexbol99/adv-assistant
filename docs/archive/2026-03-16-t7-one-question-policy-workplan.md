# T7 Detailed Workplan - One-Question Policy Engine

## Goal

Implement `T7` from `../workplan.md`:

> Implement one-question policy engine with dynamic next-question selection.

The engine must ask exactly one clarifying question at a time, pick the most relevant next question from current state, and avoid blocking generation when minimum data is already sufficient.

## Current Baseline (Code Snapshot)

The current flow already supports one-at-a-time follow-up, but the selection is mostly static:

- `src/adv_assistant/pipeline.py` uses `_next_followup_question()` with fixed order:
  - `price` -> `store_type` -> `creative_guidance`.
- `conversation_session` currently has two parallel pending-state mechanisms:
  - `pending_question_type` + `pending_question_context` (flow-v1 foundation)
  - `pending_followup_question` (stage2 follow-up implementation)
- `AdDraft.generation_ready` exists in schema but is not yet actively computed and used as the canonical decision gate.

## Scope

In scope:

- Introduce a deterministic policy engine for next-question selection.
- Make question selection dynamic based on request type, draft/operator/session state, and current pending gates.
- Keep strict "single pending question" behavior.
- Preserve current product-confirmation and publish-button behavior.
- Add automated tests for routing and regressions.

Out of scope:

- T8 prompt-composer and two-variant generation lifecycle.
- Full removal of legacy DB columns unless explicitly included as a safe follow-up migration.

## Design Principles

- Deterministic control logic first, LLM only for extraction/classification.
- Exactly one active pending question at any time.
- Ask only when needed.
- Pre-generation blocking questions and post-generation quality questions are handled by one policy interface with explicit priority rules.
- Preserve backward compatibility while migrating from `pending_followup_question` to `pending_question_type/context`.

## Implementation Plan

### Step 1 - Policy Model and Contracts

Files:

- `src/adv_assistant/pipeline.py`
- `src/adv_assistant/db/enums.py` (only if enum extension is needed)
- `src/adv_assistant/db/repositories.py` (session update helpers if needed)

Tasks:

- Define a question policy contract that returns:
  - `pending_question_type`
  - `pending_question_context`
  - `prompt_text`
  - `is_blocking_for_generation`
- Keep pending state normalized in one source (`pending_question_type/context`), with optional temporary mirroring to `pending_followup_question` for compatibility.
- Establish explicit priority order for dynamic selection:
  1. Onboarding gate
  2. Classification unresolved
  3. Product confirmation gate
  4. Missing required generation info by request type
  5. Optional quality follow-up (price/store type/creative guidance)
  6. Regenerate confirmation

Acceptance criteria:

- Policy contract can represent all active question types in the flow.
- Priority order is deterministic and documented inline.

### Step 2 - Extract Policy Engine from Pipeline

Files:

- `src/adv_assistant/pipeline.py`
- `src/adv_assistant/question_policy.py` (new)

Tasks:

- Move question-selection logic out of the main `process()` path into a dedicated engine module.
- Move helper routines currently spread across follow-up functions to policy-focused utilities.
- Replace direct `_next_followup_question()` calls with policy-engine evaluation.
- Keep existing language-aware prompt builders, but route them through policy decisions.

Acceptance criteria:

- `process()` orchestration flow is simpler and delegates next-question decisions to one component.
- Existing behavior remains unchanged where policy rules are equivalent.

### Step 3 - Dynamic Selection Rules

Files:

- `src/adv_assistant/question_policy.py`
- `src/adv_assistant/pipeline.py`

Tasks:

- Implement dynamic rule set:
  - Required-first questions vary by request type (`single_product`, `multi_product`, `store_general`).
  - For single-product flows, ask missing product-identification questions before optional optimization nudges.
  - Skip already-known fields.
  - Do not ask additional questions when generation can proceed immediately.
  - After successful generation, ask at most one optimization question at a time.
- Track asked-question context to prevent repeated prompts when user response is still ambiguous (reprompt for same key, do not branch to a new key).

Acceptance criteria:

- For each message turn, engine returns zero or one question.
- Question key changes only when previous question is resolved or interrupted by a higher-priority intent.

### Step 4 - State Unification and Compatibility

Files:

- `src/adv_assistant/db/models.py`
- `src/adv_assistant/db/repositories.py`
- `src/adv_assistant/main.py`
- `tests/test_migrations.py`
- `alembic/versions/*` (only if schema change is included in T7 scope)

Tasks:

- Prefer `pending_question_type/context` as canonical flow-v1 state.
- Compatibility strategy:
  - Phase A: keep reading legacy `pending_followup_question` but write canonical state.
  - Phase B: remove `pending_followup_question` in a dedicated migration after stability window.
- If Phase B is included now, update schema compatibility checks and migration tests in the same change set.

Acceptance criteria:

- Runtime behavior does not regress for existing sessions.
- No orphan/contradictory pending state across the two mechanisms.

### Step 5 - Tests (Primary Gate)

Files:

- `tests/test_conversation_flow_v1_t7.py` (new)
- `tests/test_stage2_system_memory.py`
- `tests/test_phase7_generation.py`
- `tests/test_repositories.py`

Test matrix:

1. Dynamic next-question chooses classification when unresolved.
2. Product confirmation overrides optional follow-up questions.
3. Required missing info is asked before optional optimization.
4. Optional follow-up asks one-by-one and never duplicates resolved keys.
5. Interrupt intent (`create_ad`, `publish_ad`, etc.) clears optional pending question and proceeds deterministically.
6. Already-enough-info path generates directly with no extra question.
7. Ambiguous answer reprompts same question key (no branch drift).
8. Session persistence stores one canonical pending question state.

Acceptance criteria:

- All new T7 tests pass.
- Existing T5/T6/Phase7 tests remain green.

### Step 6 - Validation and Rollout Safety

Commands:

- `uv run ruff check src tests`
- `uv run pytest -q tests/test_conversation_flow_v1_t5.py`
- `uv run pytest -q tests/test_stage2_system_memory.py`
- `uv run pytest -q tests/test_phase7_generation.py`
- `uv run pytest -q tests/test_conversation_flow_v1_t7.py`
- `uv run pytest -q` (full regression before merge)

Operational checks:

- Verify audit events capture question selection and resolution transitions.
- Verify publish CTA still appears after successful preview responses.
- Verify no stale-write regressions in versioned draft updates.

Acceptance criteria:

- Regression suite green.
- Manual WhatsApp smoke path confirms "one question at a time" and "direct generation when enough info."

## Deliverables Checklist

- [ ] Policy engine module added and wired.
- [ ] Dynamic next-question selection implemented.
- [ ] Canonical pending-question state enforced.
- [ ] T7 tests added and passing.
- [ ] Existing regression tests passing.
- [ ] Workplan status updated after merge.

## Risks and Mitigations

- Risk: mixed pending-state logic causes contradictory behavior.
  - Mitigation: define one canonical state and add transition assertions.
- Risk: dynamic policy accidentally blocks generation too aggressively.
  - Mitigation: explicit required-vs-optional rule split + "already enough info" tests.
- Risk: regressions in product confirmation/publish CTA paths.
  - Mitigation: keep `tests/test_phase7_generation.py` in the mandatory regression set.

## Suggested Execution Sequence

1. Add policy contract and decision object.
2. Refactor pipeline to call policy engine.
3. Add dynamic rules and resolution handlers.
4. Add/adjust tests until green.
5. Run full regression and document final behavior.
