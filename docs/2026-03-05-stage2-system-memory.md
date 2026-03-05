# Stage 2 Implementation Summary (March 5, 2026)

This document summarizes the Stage 2 MVP implementation: system memory vs draft memory for creative generation quality.

## Scope Delivered

1. Added system memory fields on `operator`.
2. Extended extraction to capture system memory from free text.
3. Wired system memory into generation input and prompt composition.
4. Added post-generation nudges to collect missing system memory.
5. Added migration + repository + pipeline + prompt tests.

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
- Generation success now appends a quality nudge when system memory is missing:
  - asks for `store_type`
  - asks for general creative guidance

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
- `tests/test_stage2_system_memory.py`
  - prompt includes system memory context
  - system memory update via `set_branding`
  - system memory persists across new drafts
  - generation follow-up asks for missing system memory

## Validation

- `uv run pytest -q` -> `108 passed`
- `uv run ruff check ...` on changed files -> passed
