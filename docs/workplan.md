# Workplan — WhatsApp Advertisement Assistant Bot

This implementation plan is execution-focused: each phase has explicit deliverables, test obligations, and an exit gate. The plan is aligned with the [Product Specification](product-spec.md), [Architecture & Technical Specification](architecture-and-technical-spec.md), and [Technology Decisions](technology-decisions.md).

---

## Delivery Principles

- Build deterministic control-plane logic first; use LLM only for language understanding and generation.
- Treat all inbound delivery paths as at-least-once; enforce idempotency and deduplication by design.
- Keep destructive actions guarded by deterministic button payload confirmation only.
- Keep authorization admin-managed only; no chat self-enrollment.
- Shift compliance checks left (before full enrichment rollout), not only at launch.
- Define "done" per phase with measurable acceptance criteria.

---

## Priority Track — Conversation Flow V1 (Active)

**Goal:** Ship the new operator conversation flow as the top-priority delivery track.

### Planning Ownership (Updated March 17, 2026)

- `docs/workplan.md` is the single source of truth for active priorities and sequencing.
- Historical execution plans and dated checklists are archived under [`docs/archive`](archive/README.md).
- When this file conflicts with an archived note, this file takes precedence.

### State Storage Contract

| Domain state | Source of truth |
|--------------|-----------------|
| Business onboarding profile | `business_profile` singleton per `business_scope` (`default` in current deployment) |
| Current request type | `ad_draft.request_type` |
| Classification resolved/not-resolved | `ad_draft.classification_status` + `ad_draft.is_classification_resolved` |
| Identified product candidates | `draft_product` rows per draft |
| Waiting for product confirmation | `ad_draft.awaiting_product_confirmation` |
| Pending question category | `conversation_session.pending_question_type` (+ context JSON) |
| Generation readiness | deterministic computation from draft/session/product state, persisted in `ad_draft.generation_ready` as orchestration cache |
| Current variant round | `ad_variant_round` (`ACTIVE` status is unique per draft) |

### Conversation Flow Contract (March 18, 2026)

This section is the authoritative reference for the MVP conversation flow. Every guard condition, state transition, and decision branch is defined here. Sprint items SP-02 through SP-10 implement these behaviors; this contract is the source of truth for expected outcomes.

#### Flow Sequence and Guard Conditions

Each step is deterministic (no LLM in control flow). Steps reference the exact DB field that gates progression.

1. **Inbound message received.**
   Session loaded (`conversation_session`); active draft loaded or created (`ad_draft`).

2. **Onboarding gate.**
   Guard: `business_profile` exists for operator's `business_scope`.
   Fail → prompt for business name + logo; `pending_question_type = ONBOARDING`.

3. **Image-first entry (SP-04).**
   Guard: inbound message is an image AND no confirmed `draft_product` exists for current draft.
   Action → use uploaded image as candidate product image; enter product confirmation flow (step 7).

4. **Out-of-flow image handling (SP-06, SP-07).**
   Guard: inbound message is an image AND draft already has a confirmed product.
   Action → ask "replace product image?" confirmation; `pending_question_type` set for image-replacement prompt.
   - **Confirm** → reset draft product fields (`product_name`, `product_brand`, `price`, draft `selected_variant_id`, `selected_round_id`, active variant round → `SUPERSEDED`). Operator-level memory is preserved (`store_type`, `creative_guidance`, currency default). Restart product flow from step 6.
   - **Cancel** → preserve current draft unchanged; resume previous flow position.

5. **Classification.**
   Guard: `ad_draft.is_classification_resolved == false`.
   Fail → ask classification question; `pending_question_type = CLASSIFICATION`.
   Resolve → set `ad_draft.classification_status = RESOLVED`, `ad_draft.is_classification_resolved = true`, `ad_draft.request_type` to selected type (`SINGLE_PRODUCT` | `MULTI_PRODUCT` | `STORE_GENERAL`).

6. **Product discovery.**
   System searches for product by name or image (retailer chain → Serper fallback).
   Result → create `draft_product` rows with `status = CANDIDATE`; set `ad_draft.awaiting_product_confirmation = true`.

7. **Product confirmation.**
   Guard: `ad_draft.awaiting_product_confirmation == true`.
   Action → show confirm/reject buttons (`BUTTON_CONFIRM_PRODUCT_SELECTION` / `BUTTON_REJECT_PRODUCT_SELECTION`).
   - **Confirm** → `draft_product.status = CONFIRMED`; clear `ad_draft.awaiting_product_confirmation`.
   - **Reject (SP-05)** → deterministic fork with two choices:
     - (a) upload a product image (returns to step 3 image-first path), or
     - (b) provide a more precise textual description (retry discovery from step 6).

8. **Generation gate (SP-02).**
   Guard: product is confirmed (`awaiting_product_confirmation == false`) AND `product_name` is present on confirmed `draft_product`.
   Fail → do not enter clarification cycle; remain in product confirmation or product-name collection.
   Pass → enter clarification cycle (step 9).

9. **Clarification cycle (SP-03, SP-10).**
   Budget: up to `MAX_CLARIFICATION_QUESTIONS` (= 3) total questions across the draft lifecycle. Questions are selected by `select_next_question()` from `question_policy.py`, asking only critical missing fields. Per-question reprompt budget is `MAX_REPROMPTS_PER_QUESTION` (= 2).
   - When enough data exists after any question (1, 2, or 3) → skip remaining questions, set `ad_draft.generation_ready = true`.
   - When budget exhausted (3 questions asked) → set `ad_draft.generation_ready = true` regardless of remaining optional fields.

10. **Generation.**
    Guard: `ad_draft.generation_ready == true`.
    Action → submit 2-variant round (`ad_variant_round` with `status = ACTIVE`, two `ad_variant` slots). If prior active round exists → transition to `SUPERSEDED`.

11. **Image precedence in generation input (SP-09).**
    Rule: if operator uploaded a draft-specific product image, that image URL overrides any discovery/enrichment image in the generation request payload.

12. **Currency source of truth (SP-08).**
    Rule: currency for generation input comes from operator/business-level default (`operator.currency` or `business_profile` currency field), not from any draft-level currency field.

13. **Variant selection → publish.**
    Operator selects variant A or B (`BUTTON_SELECT_VARIANT_A` / `BUTTON_SELECT_VARIANT_B`). Confirm-publish with idempotency guard. On publish → `ad_variant_round.status = PUBLISHED`.

#### State Field Glossary

Maps sprint terminology to exact code identifiers.

| Sprint term | Code identifier | File |
|---|---|---|
| Clarification budget (total Qs per draft) | `MAX_CLARIFICATION_QUESTIONS` (new constant, SP-03) | `question_policy.py` |
| Reprompt budget (per single question) | `MAX_REPROMPTS_PER_QUESTION = 2` | `question_policy.py` |
| Generation gate | `is_generation_ready()` | `question_policy.py` |
| Image-first message | New inbound-image handler path (SP-04) | `pipeline.py` |
| Reject-product branch | `BUTTON_REJECT_PRODUCT_SELECTION` payload | `llm_gateway.py` |
| Out-of-flow image | New image-replacement confirmation handler (SP-06) | `pipeline.py` |
| Image precedence | Generation input builder override (SP-09) | `pipeline.py` / `draft_service.py` |
| Currency ownership | `operator.currency` / `business_profile` default | `models.py` |
| Classification status | `ad_draft.classification_status` (enum: `PENDING` / `RESOLVED`) | `enums.py` |
| Classification resolved flag | `ad_draft.is_classification_resolved` (bool) | `models.py` |
| Product confirmation wait | `ad_draft.awaiting_product_confirmation` (bool) | `models.py` |
| Generation readiness cache | `ad_draft.generation_ready` (bool, computed then persisted) | `models.py` |
| Pending question type | `conversation_session.pending_question_type` (enum) | `enums.py`, `models.py` |
| Pending question context | `conversation_session.pending_question_context` (JSON dict) | `models.py` |
| Variant round status | `ad_variant_round.status` (enum: `ACTIVE` / `SUPERSEDED` / `FAILED` / `PUBLISHED`) | `enums.py` |

#### Guard Condition Summary

| Guard | DB field | Fail action |
|---|---|---|
| Onboarding complete | `business_profile` exists | Prompt for business name + logo |
| Classification resolved | `ad_draft.is_classification_resolved == true` | Ask classification question |
| Product confirmed | `ad_draft.awaiting_product_confirmation == false` | Show confirm/reject buttons |
| Product name present | `draft_product.product_name is not null` (confirmed row) | Ask for product name |
| Clarification budget | clarification count < `MAX_CLARIFICATION_QUESTIONS` | Skip remaining optional Qs, proceed to generation |
| Generation ready | `ad_draft.generation_ready == true` | Block generation, continue clarification |
| Active round uniqueness | Only one `ad_variant_round` with `status = ACTIVE` per draft | Supersede prior round before creating new one |

### Execution Backlog (V1)

| Track item | Status (main) | Notes |
|------------|---------------|-------|
| `T1` Docs + state contract | Completed | Included in V1 planning and docs updates. |
| `T2` Schema + migration foundations | Completed | Core flow-v1 schema landed (`business_profile`, session/draft state, variant tables). |
| `T3` Repository/state utilities | Completed | Repositories for variants/pending-question state are in active flow. |
| `T4` Onboarding gate (business name + logo) | Completed | Covered in onboarding flow and tests. |
| `T5` Classification loop | Completed | Unambiguous classification flow and tests are in place. |
| `T6` Product/visual discovery | Completed | Product discovery + fallback behavior integrated. |
| `T7` One-question policy engine | Completed | Canonical pending-question flow implemented (`question_policy`). |
| `T8` Dynamic prompt composer + 2 variants | Completed | Two-variant generation lifecycle is active. |
| `T9` Deterministic variant actions + round lifecycle | Completed | `ACTIVE -> SUPERSEDED` round replacement path implemented. |
| `T10` Publish idempotency | Completed | Confirm-publish retry/duplicate guards are implemented and tested. |
| `T11` E2E V1 coverage | Completed | End-to-end V1 tests cover direct generation and image flow paths. |
| `T12` Flow kill-switch | Completed | `PIPELINE_V1_ENABLED` enables full V1 vs legacy fallback. |

## MVP Priority Reset (March 5, 2026, historical baseline)

This section defines the immediate delivery order for the first MVP.
Historical detailed checklist: [MVP Priority Plan (archived)](archive/mvp-priority-plan-2026-03-05.md).

### Stage 1 — Multi-Operator CMS Routing (Highest Priority, Completed)

**Goal:** each authorized operator publishes to their own CMS campaign/playlist mapping.

**Scope**
- Add per-operator CMS mapping fields (`cms_campaign_id`, `cms_playlist_id`).
- Add a minimal admin API + web form to register/update operator CMS mapping.
- Keep publish-only scope for MVP (no list/delete-all implementation in this stage).
- If operator mapping is missing, block publish with the fixed user message:
  - "אתה לא מחובר למערכת כרגע, פנה לתמיכה כדי לייצר את החיבור"

**Test obligations**
- Migration up/down tests for operator CMS mapping fields.
- Repository tests for create/update/read mapping.
- Admin auth tests (basic username/password): unauthorized requests must fail closed.
- Integration test: two operators publish to two different campaign/playlist targets.
- Negative test: publish blocked when mapping is missing + audit event written.

**Exit gate**
- Two-number E2E publish test passes with distinct CMS targets.
- Missing-mapping publish flow returns the exact blocked message.

### Stage 2 — Creative Flow Optimization (System Memory + Draft Memory, Completed)

**Goal:** improve generation quality while preserving clear data boundaries.

**Scope**
- System memory (persists across ads): `store_type` (free text), preferred language, global creative guidance, logo.
- Draft memory (ad-specific only): product name, optional product brand, price/currency, product photo.
- Keep current generation rule: product name is required; price remains optional for first generation.
- After first generation, send short nudges to collect more context (for example store type).

**Test obligations**
- System memory persists across new drafts.
- Draft product fields do not leak between ads.
- Logo image routing remains separate from product photo routing.
- Generation input integration test verifies merged system + draft context.
- Regression tests for create/regenerate/publish paths.

**Exit gate**
- End-to-end flow proves: first generation with product name only, then follow-up prompts for quality improvements.

### Stage 3 — MVP QA and Release (Completed)

**Goal:** validate publish-ready MVP behavior.

**Scope**
- Full regression run (`pytest`, `ruff`).
- Manual WhatsApp E2E validation for:
  - connected operator publish,
  - unconnected operator publish block,
  - logo upload,
  - product image upload,
  - new ad without product-field inheritance.

**Exit gate**
- Regression suite green and manual checklist completed in staging.

### Next Sprint Backlog Additions (March 6, 2026, Completed)

**Requested carry-over items**
- Publish CTA must always be present immediately after any successful preview generation response.
- Admin operator lookup should present user data in a structured view (table/card) for faster support usage.

**Validation targets**
1. Automated tests confirm publish buttons are present for preview success + follow-up paths.
2. Manual WhatsApp E2E confirms button visibility in real conversation flow.
3. Admin UI lookup renders a structured operator profile view from phone lookup.
4. Optional manual cross-checks (Hebrew/English/regenerate-declined CTA visibility) remain optional and are not release blockers.

---

## Phase 0 — Program Controls and Bootstrap

**Goal:** Establish delivery controls, tooling, and cloud baseline.

### Scope
- Initialize Python project (`uv`, `pyproject.toml`, Python 3.12+).
- Configure `ruff`, `pytest`, `pytest-asyncio`, pre-commit, Dockerfile, local compose.
- Provision GCP services: Cloud Run, Cloud Tasks, Cloud SQL, GCS, Secret Manager.
- Create GCS bucket (`me-west1`) with public object read and 90-day lifecycle rule.
- Create Cloud Tasks queue (`me-central1`) with retry/backoff policy.
- Create environments: `development`, `staging`, `production`.
- CI pipeline: lint -> test -> build -> deploy (staging).

### Exit Criteria
- `make test` and lint run green in CI.
- Cloud resources are provisioned and documented in runbook.
- Secrets are stored in Secret Manager only (no plaintext in repo).

---

## Phase 1 — Data Model and Persistence Foundation

**Goal:** Implement relational model and persistence utilities.

### Scope
- Implement SQLAlchemy models and migrations for:
  - `operator` (authorization source of truth; `active=true` means authorized)
  - `conversation_session`
  - `ad_draft` (includes `operator_phone`, `version`)
  - `published_ad`
  - `system_config`
  - `audit_event`
  - `processed_inbound_message` (dedup by `wamid`, 30-day retention)
- Implement DB session utilities and repository layer.
- Add cleanup jobs/policies for 30-day/90-day/13-month retention controls.

### Exit Criteria
- Initial migration applies cleanly in local/staging.
- CRUD tests pass for all core entities.
- Retention jobs are defined and tested in staging.

---

## Phase 2 — Ingress Security and Message Pipeline

**Goal:** Securely receive WhatsApp events and enqueue deterministic processing.

### Scope
- Implement `GET /webhook` verification and `POST /webhook` event intake.
- Validate `X-Hub-Signature-256`.
- Enforce replay protection window: 5 minutes (when event timestamp available).
- Apply unauthorized-number policy:
  - generic rejection once per number per 60-minute window,
  - repeated attempts in window are silently ignored.
- Enqueue inbound events to Cloud Tasks.
- Protect `POST /tasks/process-message` with Cloud Tasks OIDC validation only.
- Deduplicate by `wamid` before business handling; skip already processed IDs.

### Exit Criteria
- Forged signature tests fail closed.
- Non-OIDC task invocation is rejected.
- Duplicate webhook/task replay tests prove single business execution.
- Unauthorized handling behavior matches policy (once-per-window then silent).

---

## Gate A — Secure Ingress Ready

**Required evidence**
- Security tests for signature/OIDC/replay/dedup all green.
- Architecture decision records updated with ingress controls.

---

## Phase 3 — Authorization, Session, and Draft Ownership

**Goal:** Implement authorized multi-operator behavior with private drafts.

### Scope
- Enforce admin-only operator onboarding/offboarding.
- Remove any auto-enrollment behavior for unknown numbers.
- Load and persist per-operator `conversation_session`.
- Enforce private draft ownership per operator.
- Add optimistic concurrency (`version` check): first-write-wins.
- Define stale-write user response flow.

### Exit Criteria
- Unknown number cannot create operator/session or draft.
- Authorized operator first message creates session.
- Concurrent draft update test shows first-write-wins with stale warning.

---

## Phase 4 — Intent and LLM Boundary

**Goal:** Establish robust NLU while keeping control actions deterministic.

### Scope
- Implement `LLMGateway` methods for classification/extraction/reply generation.
- Keep button-confirmation callbacks out of LLM path.
- Keep supported intents for free-text path only.
- Enforce strict schema parsing and validation for all LLM outputs.
- Keep prompt-injection controls and field constraints active.

### Exit Criteria
- Confirmation button payloads are resolved deterministically without LLM.
- Schema mismatch responses are rejected/retried safely.
- Injection test set does not trigger forbidden actions.

---

## Phase 5 — Enrichment Pipeline (Compliance-First)

**Goal:** Deliver enrichment with legal/compliance and storage controls in place.

### Scope
- Barcode path: deterministic decoder -> vision fallback.
- Provider chain: Open Food Facts -> EAN fallback -> web search enrichment.
- Persist only normalized enrichment fields; do not persist raw provider payloads.
- Notify enrichment-unavailable at most once per draft.
- Run short legal/compliance review before production enablement of enrichment sources.
- Compliance sign-off is not a blocker for MVP feature completion, but it is mandatory before production rollout with enrichment enabled.

### Exit Criteria
- Provider adapters return normalized schema consistently.
- Raw provider payload persistence is absent by design and test.
- Compliance review checklist completed and signed off.

---

## Phase 6 — Media Lifecycle and Storage

**Goal:** Implement media handling and lifecycle behavior.
**Status (March 16, 2026): Completed.**

### Scope
- Build `MediaStore` for GCS upload/read URL generation.
- Use UUID object naming with public URL output.
- Confirm 90-day lifecycle for stored media.
- Implement operator photo ingest from Meta media endpoint.

### Exit Criteria
- Media upload/read path validated end-to-end.
- Lifecycle policy verified in bucket configuration.

---

## Phase 7 — Ad Generation Integration (Nano Banana)

**Goal:** Integrate asynchronous ad generation contract.
**Status (March 17, 2026): Completed for MVP flow.**

### Scope
- Implement `NanoBananaClient` with:
  - `Authorization: Bearer <token>`
  - model `nanobanana-2`
  - `aspect_ratio` derived from requested resolution (`1920x1080` -> `16:9`)
  - stable `idempotency_key`
- Implement job submission and status polling cycle until terminal state.
- Persist generation state in draft lifecycle.

### Exit Criteria
- Polling path completes successfully end-to-end.
- Retry behavior does not create duplicate jobs for same logical request.
- Failed generation states are surfaced to operator cleanly.

---

## Gate B — Draft-to-Preview Ready

**Required evidence**
- Authorized operator can create draft, enrich, generate preview.
- Duplicate and retry scenarios do not duplicate side effects.

---

## Phase 8 — CMS Integration and Contract Safety

**Goal:** Deliver publish/list/delete-all with safe fallbacks.
**Status (March 17, 2026): Partially completed for MVP (`publish_ad` done; `list_ads` / `delete_all_ads` side effects deferred to Post-MVP).**

### Scope
- Implement `CMSClient` (`publish_ad`, `list_ads`, `delete_all_ads`).
- Keep append-only semantics and delete-all behavior.
- Add retries and timeout behavior per spec.
- Build local/staging mock CMS implementing `/api/ads` contract.
- Add contract parity tests between mock and expected production schema.

### Exit Criteria
- Publish/list/delete-all flows pass against mock and staging CMS endpoint.
- Error and timeout behavior is deterministic and user-visible.

---

## Phase 9 — Admin Control Plane

**Goal:** Provide secure admin interface and operator management.

### Scope
- Implement Admin API:
  - operator CRUD/status
  - config read/write
  - ads overview
  - audit retrieval
- Authentication at launch: username/password (secure credential store).
- CSRF protection and idle timeout.
- Admin UI screens: operator management (incl. last activity), config, ads, audit.

### Exit Criteria
- Unauthorized admin access attempts are rejected.
- Operator onboarding/offboarding works and is audit-logged.
- Authorization changes take effect immediately for webhook path.

---

## Gate C — Operational Control Ready

**Required evidence**
- Control plane can fully manage authorized operators and system config.
- All admin actions produce audit entries with actor identity.

---

## Phase 10 — Hardening and Reliability

**Goal:** Production-grade reliability, observability, and incident readiness.

### Scope
- Structured logging with trace/operator/intent context.
- Dashboards and alerts:
  - webhook latency
  - queue depth
  - generation success/failure
  - CMS publish success
- Security hardening:
  - HTTPS only
  - least-privilege IAM
  - secret rotation
  - rate limits
  - unauthorized traffic throttling
- Incident runbooks:
  - compromised operator response (immediate deactivation)
  - queue backlog response
  - dependency outage handling

### Exit Criteria
- Alerting fires in synthetic failure drills.
- Incident runbooks are exercised at least once in staging.

---

## Phase 11 — E2E Verification and Launch

**Goal:** Validate end-to-end behavior and launch safely.

### Scope
- End-to-end tests for:
  - authorized operator first contact -> session creation
  - draft->preview->publish
  - regeneration (reference + fresh)
  - delete-all with button confirmation
  - duplicate delivery and retry idempotency (`wamid`)
- Security verification:
  - webhook signature rejection
  - OIDC task endpoint enforcement
  - replay-window enforcement
  - admin auth enforcement
- Final compliance gate for enrichment-source terms.
- Production smoke test with real WhatsApp and CMS.

### Exit Criteria
- E2E suite green in staging.
- Security verification checklist complete.
- Compliance checklist complete.
- Production smoke test successful.

---

## Milestone Summary

| Milestone | Completion Point | Outcome |
|-----------|------------------|---------|
| M1 | Gate A | Secure, idempotent ingress pipeline |
| M2 | Gate B | Reliable draft-to-preview generation flow |
| M3 | Gate C | Operational admin control plane live |
| M4 | Phase 11 done | Launch-ready production system |

---

## Current Repository Status (as of March 18, 2026)

Based on the current codebase and automated test coverage:

- **Planning ownership:** this file is now the only active planning source; dated task docs are archived under `docs/archive/`.
- **Feature completion status:** the V1 track (`T1`..`T12`) is implemented on `main` for MVP flow.
- **Gate A / Gate B evidence:** present in tests (`tests/test_ingress_pipeline.py`, `tests/test_tasks_auth.py`, `tests/test_phase4_llm_boundary.py`, `tests/test_phase5_enrichment.py`, `tests/test_phase6_media.py`, `tests/test_phase7_generation.py`, `tests/test_conversation_flow_v1_e2e.py`).
- **MVP scope now:** keep `publish_ad` flow active; keep `list_ads` and `delete_all` execution as Post-MVP scope.
- **Compliance status:** Phase 5 compliance checklist is still open; it does not block feature completion, but it must be closed before production enrichment enablement.
- **Production readiness still pending:** Phase 9 (full admin control plane), Phase 10 (hardening), and Phase 11 (launch verification).

---

## Immediate Next Actions

1. Finalize MVP feature scope freeze on `main`: keep current flow as MVP baseline and keep `list_ads` / `delete_all` side effects in Post-MVP backlog.
2. Run full MVP stabilization pass (`ruff`, `pytest`, targeted staging smoke) and close any regressions before release candidate tagging.
3. Prepare production rollout track in parallel: Phase 9 minimum operational controls, then Phase 10 hardening essentials, then Phase 11 launch checks.
4. Complete Phase 5 compliance checklist before enabling enrichment in production (`docs/phase5-compliance-checklist.md`).
5. Keep optional manual CTA cross-checks as optional (non-blocking) verification items.

---

## Current Sprint Plan — MVP Feature Completion (March 18, 2026)

Objective: complete MVP feature behavior for product-image control and clarification flow, then stabilize for production rollout.

### Locked Functional Decisions

1. Flow sequence:
   - operator sends create prompt
   - system searches product
   - system asks for product confirmation
   - operator confirms product
   - system asks up to 3 clarification questions
   - system generates final ad
2. Clarification budget is **up to 3** questions (not always exactly 3).
3. `product_name` must exist before entering the 3-question clarification cycle.
4. Operator-uploaded product image is draft-scoped (current draft only), not global memory.
5. Currency target state is operator/business-level default (not draft-level source of truth).
6. If operator sends an image outside the expected image steps, system must ask confirmation before replacing current product image and must reset the draft on confirmed replacement.
7. When product confirmation is rejected after system lookup, system offers two deterministic paths:
   - upload product image
   - provide more precise textual description (then retry lookup + confirmation).

### Sprint Backlog (Testable Work Items)

| ID | Priority | Task | Acceptance checks |
|----|----------|------|-------------------|
| `SP-01` | P0 | Write the flow/state contract for the March 18 behavior in this workplan and align terms used in code/tests. | Updated docs clearly define the sequence and guard conditions; no conflicting active task doc remains outside `docs/archive`. |
| `SP-02` | P0 | Add a generation gate: clarification cycle starts only after product is confirmed and `product_name` is present. | Integration test proves no entry to clarification cycle before product confirmation + product name availability. |
| `SP-03` | P0 | Implement clarification budget (`<= 3`) in deterministic question policy. | Conversation test proves no 4th clarification question; when enough data exists after question 1/2/3 generation starts immediately. |
| `SP-04` | P0 | Implement "image-first message" path: operator can start flow by sending a product image. | E2E test starts with image payload and reaches product confirmation flow successfully. |
| `SP-05` | P0 | Implement deterministic reject-product branch: ask operator to upload image or provide a more precise description. | Tests cover both branch choices and validate state transitions + prompts. |
| `SP-06` | P0 | Implement out-of-flow image handling: ask "replace product image?" confirmation. | Confirm branch replaces image; cancel branch preserves current draft image; both paths tested. |
| `SP-07` | P0 | On confirmed out-of-flow image replacement, reset current draft product-specific fields and restart product flow. | Test verifies reset scope includes product fields, price-related fields, selected variant/round context; operator-level memory remains unchanged. |
| `SP-08` | P0 | Move currency ownership to operator/business-level default and remove draft-level currency as source of truth for generation decisions. | Migration/repository tests validate source of truth; generation input tests validate currency comes from operator/business default unless explicitly overridden by business-level update flow. |
| `SP-09` | P0 | Enforce image precedence in generation input: user-uploaded draft image overrides discovery/enrichment images. | Generation integration test verifies submitted image URL equals user-uploaded image when available. |
| `SP-10` | P0 | Refine image-generation system prompt and policy interplay: ask only critical missing clarifications and respect 3-question cap. | Prompt contract test (or snapshot) + behavior tests confirm no extra non-critical clarifications and no question 4. |
| `SP-11` | P1 | Add audit/trace events for new paths (image override request/confirm/cancel, clarification budget reached, regenerate after precise description). | Audit event tests confirm expected events and metadata in each path. |
| `SP-12` | P1 | Run stabilization + release checks for updated MVP behavior. | `ruff` and `pytest` are green; staging smoke verifies image override, 3-question cap, and publish flow regression-free. |

### Explicit Post-MVP Scope (Unchanged)

- `list_ads` and `delete_all` side-effect execution remain Post-MVP scope.
