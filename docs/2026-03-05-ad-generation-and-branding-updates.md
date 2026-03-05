# Ad Generation and Branding Updates (2026-03-05)

## Summary
This change set improves ad-creation reliability and business-branding behavior in the WhatsApp flow.

Main goals:
- Preserve operator branding data (business name, logo, colors).
- Separate "new ad" from previous ad edits by creating a fresh draft.
- Route logo uploads correctly (logo vs. product photo).
- Improve product understanding (`product_name`, `product_brand`, `price`).
- Generate a first preview faster, even when price is missing, then request missing details.
- Reduce transient Gemini provider failures with configurable retry controls.

## Functional Changes

### 1) Operator branding persistence
- Operator now stores:
  - `business_name`
  - `logo_url`
  - `brand_colors`
- Branding can be updated from conversation and is injected into generation input.

### 2) Product brand extraction and precedence
- Added `product_brand` as an extracted and persisted ad field.
- If `product_brand` (operator-provided) conflicts with `enriched_brand` (catalog/EAN):
  - First preview uses `product_brand`.
  - Follow-up clarification message is sent.

### 3) New-ad isolation
- `Intent.CREATE_AD` now creates a new draft (instead of reusing old product state).
- Previous ad modifications do not leak into new ad requests.

### 4) Logo upload routing fix
- Replaced history-flag-based routing with explicit session state (`pending_upload_type`).
- The next image after logo intent is routed to operator logo storage.
- Non-logo image flow remains product-photo ingestion.

### 5) Faster first preview behavior
- Generation now requires product name (price is no longer a hard blocker).
- If price is missing:
  - System still generates a first preview.
  - System asks for exact price afterward to improve next revision.
- If product name is missing:
  - System asks for product name explicitly.

### 6) Gemini reliability hardening
- Added configurable Gemini retry knobs:
  - `GEMINI_MAX_SUBMIT_ATTEMPTS`
  - `GEMINI_RETRY_BASE_SECONDS`
- Wired settings into Gemini service initialization.
- Existing timeout behavior remains configurable with `GEMINI_TIMEOUT_SECONDS`.

## Database / Migrations
- Added migration: `20260304_0003_operator_branding.py`
  - Adds operator branding columns.
- Added migration: `20260304_0004_product_brand_and_pending_upload.py`
  - Adds `ad_draft.product_brand`.
  - Adds `conversation_session.pending_upload_type`.

## Startup schema validation
Startup schema compatibility now verifies required columns for:
- `operator`
- `conversation_session`
- `ad_draft`
(and enrichment columns when enrichment is enabled).

## Test Coverage Added/Updated
- Migration coverage for new columns.
- Repository coverage for branding update and session `pending_upload_type`.
- Pipeline tests for:
  - logo upload routing,
  - new draft creation on new ad,
  - brand conflict behavior,
  - generation without price + follow-up.

## Validation Performed
- `uv run ruff check src tests`
- `uv run pytest -q`

All tests passed locally.

## Operational Notes
Recommended runtime values for better resilience on unstable Gemini responses:
- `GEMINI_TIMEOUT_SECONDS=90`
- `GEMINI_MAX_SUBMIT_ATTEMPTS=5`
- `GEMINI_RETRY_BASE_SECONDS=1.5`

After deployment:
1. Run migrations (`uv run alembic upgrade head`).
2. Restart the service.
3. Monitor audit events:
   - `generation_job_submitted`
   - `generation_completed`
   - `generation_flow_failed`
   - `product_brand_conflict_detected`
