# MVP Priority Plan (March 5, 2026)

This document captures the agreed MVP execution order, acceptance rules, and test checklist.

## Locked Product Decisions

1. If operator CMS mapping is missing, publishing is blocked with:
   - "אתה לא מחובר למערכת כרגע, פנה לתמיכה כדי לייצר את החיבור"
2. MVP publish scope is publish-only (no `list_ads` / `delete_all` implementation in this iteration).
3. First generation requires only product name.
4. Price remains optional for first generation (same behavior as current flow).
5. `store_type` is free text.
6. MVP includes a basic admin interface (Basic Auth + minimal web form) to connect operator -> CMS mapping.

## Current Status (March 5, 2026)

- Stage 1 implementation is complete in code and tests.
- Stage 2 core implementation (system memory modeling/extraction/generation wiring) is complete in code and tests.
- Stage 3 remains release validation once Stage 2 is merged.

## Stage 1 — Multi-Operator CMS Routing + Basic Admin

### Deliverables
- Add per-operator CMS routing fields:
  - `cms_campaign_id`
  - `cms_playlist_id`
  - optional `meta_user_id`
- Add admin API endpoints for mapping setup/update.
- Add minimal web admin form for mapping setup/update.
- Route publish by operator mapping only (no global fallback for missing mapping).
- Emit audit events for mapping updates and blocked publish attempts.

### Tests
1. Migration tests (`up`/`down`) for operator mapping fields.
2. Repository tests for create/update/read mapping.
3. Admin auth tests:
   - unauthenticated request rejected
   - wrong credentials rejected
4. Admin endpoint tests:
   - valid connect/update succeeds
   - invalid payload rejected
   - unknown operator handling
5. Integration tests:
   - operator A publishes to campaign/playlist A
   - operator B publishes to campaign/playlist B
6. Negative integration:
   - operator without mapping gets the fixed blocked message
   - no CMS publish side effect occurs

### Exit Gate
- Two-operator publish routing E2E (staging) passes.
- Missing mapping behavior and message verified.

## Stage 2 — Creative Flow Optimization (System Memory + Draft Memory)

### Deliverables
- System memory per operator (persisted across ads):
  - `store_type` (free text)
  - preferred language
  - global creative guidance
  - logo URL
- Draft memory per ad (not persisted across ads):
  - `product_name`
  - optional `product_brand`
  - optional `price`/`currency`
  - product photo URL
- Keep generation gating:
  - required: `product_name`
  - optional: `price` for first generation
- Add post-generation quality follow-up nudges (for example asking store type/guidance).

### Tests
1. System memory persists across new drafts.
2. Draft product fields do not leak between new ads.
3. `set_logo` image is saved to operator logo memory.
4. Non-logo image is saved as draft product photo.
5. Generation input integration:
   - includes merged system + draft context
   - uses operator-provided values where precedence is defined
6. Regression tests for create/regenerate/publish flows.

### Exit Gate
- End-to-end flow proves:
  - first generation succeeds with product name only
  - follow-up prompts ask for extra context after preview

## Stage 3 — MVP QA and Release Readiness

### Deliverables
- Regression run and lint checks.
- Manual WhatsApp staging checklist execution.
- Release notes with known deferred scope.

### Tests
1. `uv run ruff check src tests`
2. `uv run pytest -q`
3. Manual WhatsApp scenarios:
   - connected operator publish success
   - unconnected operator blocked publish
   - logo upload flow
   - product photo upload flow
   - new ad isolation (no field leakage)

### Exit Gate
- Automated regression green.
- Manual checklist signed in staging.

## Deferred Scope (Post-MVP)

- `list_ads`
- `delete_all` side-effect implementation
- full admin control plane scope (beyond mapping UI/API)
- Phase 10 hardening and Phase 11 launch gates

## Next Sprint Additions (March 6, 2026)

### 1) Publish CTA consistency hardening
- Ensure publish buttons are always sent right after any successful ad preview response.
- Keep this guarantee across follow-up states (missing data questions, regenerate confirmation, regenerate-declined path).

#### Test checklist
1. Create ad -> preview reply includes publish buttons.
2. Create ad with pending follow-up -> same preview reply still includes publish buttons.
3. Regenerate confirmation answered with "no" -> decline reply includes publish buttons.
4. Regression: manual WhatsApp E2E verifies button visibility for both Hebrew and English operators.

### 2) Admin UX for operator data visibility
- Add a structured operator profile presentation in admin (table/card layout, not raw JSON-only), focused on fast support diagnostics.
- Keep existing API responses, and improve presentation layer for:
  - identity and routing (`phone`, `meta_user_id`, campaign/playlist)
  - branding/system-memory fields
  - status and timestamps

#### Test checklist
1. `/admin` renders structured operator data view after lookup.
2. View displays empty/null fields clearly without breaking layout.
3. Lookup by phone and by meta user ID both populate the same structured view.
4. Basic auth protection remains unchanged.
