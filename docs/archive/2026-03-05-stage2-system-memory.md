# Stage 2 Implementation Summary (March 5, 2026)

This document summarizes the Stage 2 MVP implementation: system memory vs draft memory for creative generation quality.

## Scope Delivered

1. Added system memory fields on `operator`.
2. Extended extraction to capture system memory from free text.
3. Wired system memory into generation input and prompt composition.
4. Added sequential one-question-at-a-time follow-up flow for missing data.
5. Added regenerate confirmation after follow-up completion.
6. Added chat clear commands for operator memory fields (`logo`, `store_type`, `creative_guidance`, etc.).
7. Added `conversation_session.pending_followup_question` state tracking with migration/repository support.
8. Added LLM gateway fallback for models that reject explicit `temperature=0` (for `gpt-5-mini` compatibility).
9. Added migration + repository + pipeline + prompt tests.

## Data Model

### New `operator` fields
- `store_type` (nullable string, up to 120 chars)
- `creative_guidance` (nullable string, up to 500 chars)

### Migration
- `alembic/versions/20260305_0006_operator_system_memory.py`

## LLM Extraction Updates

`ExtractedBrandingFields` now supports:
- `store_type`
- `creative_guidance`
- `preferred_language` (normalized to operator `language`, for example `he`, `en`, `ar`, `ru`)

Intent classification guidance was updated so business-profile messages map to `set_branding`.

## Pipeline Behavior

- `set_branding` now updates:
  - `business_name`
  - `brand_colors`
  - `store_type`
  - `creative_guidance`
  - `language` (when provided)
- Generation success now sets a single pending follow-up question at a time:
  - order: `price` -> `store_type` -> `creative_guidance`
- After each answer, only the next missing question is asked.
- After all follow-up questions are complete, system asks whether to run another generation now.
- When user confirms another generation, input update extraction runs first and only then generation is triggered.
- When a command-like intent is detected during pending follow-up, follow-up is interrupted and command flow takes precedence.
- Publish CTA buttons are sent after preview generation, including cases where follow-up is still pending.
- If user declines regenerate confirmation, publish CTA buttons are sent again with the decline response.
- Chat now supports explicit clear requests for operator fields (`logo`, `store_type`, `creative_guidance`, `business_name`, `brand_colors`) and logs the action.

## Generation Integration

`GenerationDraftInput` now includes:
- `store_type`
- `creative_guidance`

Prompt builder includes these values under business branding context when present.

## Test Coverage Added/Updated

- `tests/test_migrations.py`
  - verifies Stage 2 columns on head
  - verifies downgrade removes Stage 2 columns
- `tests/test_repositories.py`
  - verifies repository updates for system memory + language
- `tests/test_phase4_llm_boundary.py`
  - validates normalization/validation of branding system-memory fields
  - validates OpenAI retry fallback when model rejects explicit `temperature=0`
- `tests/test_stage2_system_memory.py`
  - prompt includes system memory context
  - system memory update via `set_branding`
  - system memory persists across new drafts
  - generation follow-up asks one question at a time
  - regenerate confirmation behavior and interrupt behavior
  - publish CTA appears after generated previews, including follow-up flows
  - clear requests remove selected operator memory fields

## Validation

- `uv run pytest -q` -> `117 passed`
- `uv run ruff check ...` on changed files -> passed
