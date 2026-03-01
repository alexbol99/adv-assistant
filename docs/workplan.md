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

### Exit Criteria
- Provider adapters return normalized schema consistently.
- Raw provider payload persistence is absent by design and test.
- Compliance review checklist completed and signed off.

---

## Phase 6 — Media Lifecycle and Storage

**Goal:** Implement media handling and lifecycle behavior.

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

### Scope
- Implement `NanoBananaClient` with:
  - `Authorization: Bearer <token>`
  - model `nanobanana-2`
  - `aspect_ratio` derived from requested resolution (`1920x1080` -> `16:9`)
  - stable `idempotency_key`
- Implement job submission, status polling, and callback handling.
- Verify callback authenticity via shared-secret signature.
- Persist generation state in draft lifecycle.

### Exit Criteria
- Polling and callback paths both complete successfully.
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

## Immediate Next Actions

1. Implement Phase 2 webhook ingress endpoints (`GET /webhook`, `POST /webhook`) with signature verification and replay-window checks.
2. Integrate Cloud Tasks enqueue + OIDC-protected task processor path, and enforce `wamid` dedup before business handling.
3. Prepare Gate A evidence package (security tests + updated ingress ADRs) before starting Phase 3.
