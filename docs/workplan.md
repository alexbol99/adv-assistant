# Workplan — WhatsApp Advertisement Assistant Bot

This document describes the step-by-step implementation plan for building the WhatsApp Advertisement Assistant Bot. It is aligned with the [Product Specification](product-spec.md), [Architecture & Technical Specification](architecture-and-technical-spec.md), and the decisions captured in [Technology Decisions](technology-decisions.md).

---

## Phase 0 — Project Setup & Infrastructure Bootstrap

**Goal:** Establish the project skeleton, CI pipeline, and cloud infrastructure so that every subsequent phase can deploy and test in a realistic environment from day one.

### 0.1 Repository & Tooling
- Initialise Python project with `uv` and `pyproject.toml` (Python 3.12+).
- Configure `ruff` for linting and formatting; enforce on every commit via pre-commit hooks.
- Set up `pytest` + `pytest-asyncio` for testing.
- Create a `Dockerfile` (slim Python base image) and `docker-compose.yml` for local development.

### 0.2 GCP Project Bootstrap
- Create a GCP project and enable required APIs: Cloud Run, Cloud Tasks, Cloud SQL, GCS, Secret Manager.
- Provision a Cloud SQL PostgreSQL instance (region: `me-west1`).
- Create a GCS bucket with uniform public access (`allUsers objectViewer`), `me-west1` region, and a 90-day lifecycle delete rule.
- Create a Cloud Tasks queue (`me-west1`) with max retries = 5, min back-off = 5 s, max back-off = 300 s.
- Store all secrets (DB password, API keys, webhook tokens) in Google Secret Manager.

### 0.3 CI/CD Pipeline
- Configure GitHub Actions (or equivalent) for: lint → test → build Docker image → deploy to Cloud Run (staging).
- Set up separate environments: `development` (local), `staging` (Cloud Run), `production` (Cloud Run).

---

## Phase 1 — Database Schema & ORM Foundation

**Goal:** Define and migrate the relational schema that underpins the entire application.

### 1.1 SQLAlchemy Models
- Define async SQLAlchemy 2 models for all tables (see arch spec §5):
  - `operator` — registered WhatsApp phone numbers, language/currency preferences.
  - `conversation_session` — per-operator session state as JSONB.
  - `ad_draft` — advertisement in progress or completed (fields: `operator_phone`, product name, price, EAN, promo text, photo URL, `generation_job_id`, `preview_reference_url`, `rendered_image_url`, `version`, status).
  - `published_ad` — immutable record of CMS publish events.
  - `system_config` — singleton configuration row (CMS URL, defaults).
  - `audit_event` — append-only log of admin and bot actions.
  - `processed_inbound_message` — dedup store keyed by WhatsApp `wamid` with retention metadata (30-day TTL).

### 1.2 Alembic Migrations
- Initialise Alembic; generate the initial migration from the models.
- Configure Cloud SQL Auth Proxy connection (Unix socket, `asyncpg` driver) for staging and production.

### 1.3 Database Utilities
- Implement `get_db_session()` async context manager.
- Implement basic CRUD helpers for `operator` and `conversation_session`.

---

## Phase 2 — WhatsApp Webhook Integration

**Goal:** Receive and validate inbound WhatsApp messages; acknowledge immediately; enqueue for async processing.

### 2.1 FastAPI Application Shell
- Initialise a FastAPI application with health-check endpoint (`GET /health`).
- Configure application startup/shutdown lifespan events (DB pool, Cloud Tasks client).

### 2.2 Webhook Endpoint
- Implement `GET /webhook` for Meta's verification handshake (challenge–response).
- Implement `POST /webhook` to receive inbound message events.
- Verify `X-Hub-Signature-256` HMAC signature on every inbound request; reject invalid signatures with HTTP 403.
- Reject stale inbound events outside a configured replay window (default: 5 minutes) when message timestamps are available.
- Apply unauthorised-number policy: send a generic rejection once per number per configured window, then silently ignore repeated attempts in that window.
- Return HTTP 200 immediately after enqueuing the message task.

### 2.3 Cloud Tasks Enqueue
- Serialise the incoming message payload as JSON.
- Enqueue a Cloud Tasks HTTP task targeting `POST /tasks/process-message` on the same Cloud Run service.
- Implement the `POST /tasks/process-message` endpoint that triggers the Conversation Manager.
- Require OIDC-authenticated Cloud Tasks invocations for `POST /tasks/process-message`; reject unauthenticated callers.
- Deduplicate by WhatsApp `wamid` before processing; skip already-processed messages and persist dedup records for 30 days.

---

## Phase 3 — Conversation Manager & Session State

**Goal:** Maintain per-operator conversation context across turns using the database.

### 3.1 Session Loading & Persistence
- On each task invocation, load the operator's `conversation_session` from the database (keyed by phone number).
- After processing, write updated session state back to the database.
- Auto-create an `operator` record on first contact.
- Enforce admin-only operator onboarding: no self-enrollment via chat.

### 3.2 Conversation History
- Store the last N message turns (configurable) as a list in `conversation_session.context` (JSONB).
- Provide helpers to append a turn, retrieve history for LLM context, and clear/reset the session.

### 3.3 Session State Machine
- Define the high-level session states: `idle`, `collecting_product_info`, `awaiting_confirmation`, `generating_ad`, `awaiting_publish_confirmation`.
- Implement state transitions driven by intent classification (Phase 4).
- Keep drafts private per operator (no shared draft editing between operators).
- Apply optimistic concurrency (`version` or `updated_at`) for draft updates; first-write-wins and stale writers receive a refresh warning.

---

## Phase 4 — LLM Gateway & Intent Classification

**Goal:** Integrate the LLM to parse operator messages, classify intents, and generate natural-language replies.

### 4.1 LLM Client Setup
- Configure the OpenAI Python SDK (GPT-4o or equivalent) with API key from Secret Manager.
- Implement a `LLMGateway` class with methods: `classify_intent()`, `extract_product_fields()`, `generate_reply()`.

### 4.2 Intent Classification
- Define supported intents (see arch spec §3):
  - `create_ad`, `publish_ad`, `confirm_publish`, `reject_draft`, `regenerate_with_reference`, `regenerate_from_scratch`, `delete_all`, `confirm_delete_all`, `list_ads`, `help`, `set_language`, `unknown`.
- Prompt the LLM to classify the operator's message and return a structured JSON intent object.

### 4.3 Field Extraction
- Prompt the LLM to extract structured fields from free-form operator messages:
  - `product_name`, `price`, `currency`, `ean`, `promo_text`.
- Merge extracted fields into the current `AdDraft` (existing values take precedence unless the operator explicitly changes them).

### 4.4 Prompt-Injection Guardrails
- Wrap all operator-supplied text in clear data delimiters in system prompts (e.g., `<operator_input>…</operator_input>`).
- Include an instruction prohibiting the LLM from treating delimited content as instructions.
- Validate that LLM responses match expected schemas; discard and retry on schema mismatch.

### 4.5 Multi-Language Support
- Read the operator's stored language preference (`operator.language`).
- Include the language preference in all prompts so the LLM replies in Hebrew, English, Russian, or Arabic as appropriate.

---

## Phase 5 — Product Enrichment Pipeline

**Goal:** Enrich ad content with product data from barcodes, structured databases, and web search.

### 5.1 Barcode Decoding
- Implement two-stage barcode extraction from operator-uploaded product photos:
  1. **Primary**: deterministic decoder using `zxing-cpp` (Python bindings) or `pyzbar` (zbar); supports EAN-13, EAN-8, UPC-A.
  2. **Fallback**: vision-LLM (GPT-4o with vision) when the deterministic decoder finds no barcode.
- Store the decoded EAN in `AdDraft.ean`.

### 5.2 EAN Product Lookup
- Implement the three-provider lookup chain:
  1. **Open Food Facts** (`https://world.openfoodfacts.org/api/v2/product/{ean}`) — primary.
  2. **EAN-Search.org** (or equivalent) — fallback when Open Food Facts returns no result.
  3. **Web search enrichment** (Search API targeting Hebrew-language Israeli retailer pages, e.g., Shufersal, Rami Levy, Victory) — used when structured databases return sparse data.
- Map provider responses to the `EnrichedProduct` dataclass:
  ```python
  class EnrichedProduct:
      product_name: str | None
      brand: str | None
      category: str | None
      description: str | None
      image_url: str | None
      source: str  # "open_food_facts" | "ean_search" | "web" | "none"
  ```
- Merge enriched fields into `AdDraft`; operator-provided values always take precedence.

### 5.3 Graceful Degradation
- If all providers fail or return no data, proceed with only operator-provided fields.
- Notify the operator once per draft that automatic enrichment was unavailable.
- Do not persist raw provider responses; persist only normalized enrichment fields used by the draft.

---

## Phase 6 — Media Storage (GCS)

**Goal:** Upload and manage ad images and product photos in GCS with public URLs and automatic TTL cleanup.

### 6.1 GCS Client
- Implement a `MediaStore` class wrapping the `google-cloud-storage` Python client.
- Upload method: accept file bytes + MIME type; generate a UUID v4 object name (`{category}/{uuid4}.{ext}`); upload object and return the public URL (`https://storage.googleapis.com/<bucket>/<object>`). Public readability is managed via bucket-level IAM (`allUsers objectViewer`), not per-object ACLs.

### 6.2 URL Strategy
- Use public GCS object URLs (not signed URLs) to ensure permanent accessibility for the CMS and WhatsApp preview messages.
- Object names are cryptographically unguessable (UUID v4), providing security through obscurity.

### 6.3 Lifecycle TTL
- Confirm the 90-day lifecycle delete rule is applied at the bucket level (set up in Phase 0).
- No application-level cleanup code required.

### 6.4 Photo Handling
- When the operator sends a product photo via WhatsApp, download it from the Meta media endpoint and upload it to GCS.
- Store the GCS URL in `AdDraft.photo_url` for use in enrichment (Phase 5) and ad generation (Phase 7).

---

## Phase 7 — Ad Generation Engine (Nano Banana)

**Goal:** Integrate Nano Banana to generate advertisement images using an async job model.

### 7.1 Nano Banana Client
- Implement a `NanoBananaClient` class with methods: `submit_job()`, `poll_job()`, `register_callback()`.
- `submit_job()`: POST the structured ad data (product name, price, promo text, optional photo URL, enriched details, 1920×1080 resolution, 10% safe margins) to the Nano Banana API; return the job ID.
- `poll_job()`: GET the job status endpoint with the job ID until status is `completed` or `failed`; return the rendered image URL or binary.

### 7.2 Async Generation Flow
- The ad generation flow runs as an enqueued Cloud Tasks task (not inline in the conversation handler) to avoid blocking response latency.
- On job submission, store the job ID in `AdDraft.generation_job_id` and set status to `GENERATING`.
- On completion, download the rendered image from Nano Banana, upload it to GCS (Phase 6), store the GCS URL in `AdDraft.rendered_image_url`, set `AdDraft.preview_reference_url` to the same URL, and set status to `PREVIEW_READY`.
- Send the preview image to the operator via WhatsApp.

### 7.3 Callback Support
- Implement a `POST /callbacks/nano-banana` endpoint to receive Nano Banana job completion callbacks.
- On callback receipt, trigger the same image download → GCS upload → operator notification flow as polling.
- The decision between polling and callback mode is configurable; callback mode is preferred in production.

### 7.4 Regeneration Modes
- **Regenerate with reference**: submit previous `AdDraft.preview_reference_url` as a visual reference along with the operator's change instructions.
- **Regenerate from scratch**: submit without a reference image; fresh generation from collected `AdDraft` data.

---

## Phase 8 — TV CMS Integration

**Goal:** Implement the CMS client for publishing, listing, and deleting advertisements.

### 8.1 CMS Client
- Implement a `CMSClient` class configured with the CMS endpoint URL from `system_config`.
- Methods: `publish_ad(ad_draft)`, `list_ads()`, `delete_all_ads()`.
- `publish_ad()`: POST the ad payload (`image_url`, metadata) to the CMS append endpoint; record the result in `published_ad`.
- `list_ads()`: GET the current CMS playlist; return a summary list of active ads.
- `delete_all_ads()`: DELETE all ads from the CMS; record the action in `audit_event`.

### 8.2 Confirmation Flow
- `publish_ad` and `delete_all` intents require explicit operator confirmation via interactive WhatsApp buttons only.
- Confirmation state is tracked in `conversation_session` to prevent re-prompting on retries.
- Destructive actions cannot be bypassed by a crafted message (see arch spec §4 and product spec §9.2).

### 8.3 Audit Logging
- Write an `audit_event` record for every publish, delete-all, and admin action.

---

## Phase 9 — Admin Console

**Goal:** Build the web-based Admin Console for system configuration and operational oversight.

### 9.1 Admin API
- Implement Admin API endpoints (FastAPI router, `/admin/...`):
  - `GET/POST /admin/operators` — list and register operator phone numbers.
  - `PUT /admin/operators/{id}` — update operator status (active/deactivated).
  - `GET/PUT /admin/config` — read and update `system_config` (CMS URL, defaults).
  - `GET /admin/ads` — list currently published ads.
  - `GET /admin/audit` — retrieve audit log entries (paginated).

### 9.2 Authentication & Security
- Protect all `/admin/...` endpoints with strong authentication (username + password with secure credential store at launch; OIDC/SSO preferred for maturity).
- Enforce session timeout (30-minute idle expiry).
- Apply CSRF protection on all state-changing requests.
- Log all admin actions to `audit_event`.

### 9.3 Admin UI
- Build a minimal browser-based UI (e.g., simple HTML + HTMX, or a lightweight React app) served from the same Cloud Run service.
- Screens: operator management (including last activity timestamp), CMS configuration, active ads overview, audit log.

---

## Phase 10 — Hardening, Observability & Security

**Goal:** Make the application production-ready with proper error handling, logging, monitoring, and security controls.

### 10.1 Error Handling & Retries
- Implement structured error handling throughout: distinguish transient errors (retry) from permanent errors (fail fast).
- Ensure all Cloud Tasks tasks are idempotent (safe to retry).
- Handle Nano Banana API errors: invalid input, quota exhaustion, generation failures.

### 10.2 Structured Logging
- Use structured JSON logging (e.g., `structlog`) with fields: `trace_id`, `operator_id`, `intent`, `duration_ms`, `error`.
- Forward logs to Google Cloud Logging.
- Include actor identity details in audit metadata (`operator_phone` for WhatsApp actions; `admin_user_id` for admin-console actions).

### 10.3 Monitoring & Alerting
- Define Cloud Monitoring dashboards for: webhook latency, task queue depth, ad generation success/failure rate, CMS publish success rate.
- Set up alerting for: sustained task queue backlog, high LLM error rate, Cloud Run unhealthy instances.

### 10.4 Security Hardening
- Enforce HTTPS everywhere; reject plain HTTP.
- Apply principle of least privilege to all IAM service accounts.
- Rotate secrets via Secret Manager on a defined schedule.
- Rate-limit webhook and admin API endpoints.
- Add per-operator and per-phone-number throttling for unauthorised traffic.
- Define compromised-operator response runbook: immediate operator deactivation via Admin Console and audit notification.

### 10.5 Data Retention Controls
- Enforce retention policy in storage and DB cleanup jobs:
  - Conversation history: 30 days.
  - Operator-uploaded photos / generated media: 90 days.
  - Draft-enrichment normalized fields: 30 days.
  - Processed inbound dedup records: 30 days.
  - Audit events: 13 months.

### 10.6 Performance
- Set Cloud Run minimum instances = 1 to avoid cold starts in production.
- Configure DB connection pool size appropriate for Cloud Run concurrency settings.
- Add response caching for `list_ads` and `system_config` reads where safe.

---

## Phase 11 — End-to-End Testing & Launch Preparation

**Goal:** Validate the full user journey, fix any remaining issues, and prepare for production launch.

### 11.1 Integration Tests
- Write end-to-end integration tests covering the happy path and key error paths:
  - New operator first contact → session creation.
  - Product description → enrichment → ad generation → preview delivery.
  - Regeneration (with reference and from scratch).
  - Publish → CMS record created → audit log written.
  - Delete-all → confirmation flow → CMS cleared.
  - Duplicate webhook/task delivery → single business action (dedup by `wamid`).
- Run integration tests against a staging environment backed by real GCP services.

### 11.2 Load & Resilience Testing
- Simulate concurrent operators sending messages; verify task queue handles load without message loss.
- Test Cloud Run scale-out behaviour.

### 11.3 Security Review
- Review all operator-facing inputs for prompt-injection vectors.
- Confirm webhook signature verification rejects tampered requests.
- Confirm admin endpoints reject unauthenticated requests.
- Confirm `POST /tasks/process-message` rejects non-OIDC callers.
- Confirm replay-window checks reject stale inbound events.
- Run a short legal/compliance review of enrichment-source terms before production go-live.

### 11.4 Documentation & Runbook
- Write operator onboarding guide (how to register a phone number, connect the CMS).
- Write ops runbook (deployment, secret rotation, incident response, DB backup/restore).
- Include operator onboarding/offboarding checklist and unauthorised-access handling policy in the runbook.

### 11.5 Production Launch
- Cut production Cloud Run deployment.
- Verify end-to-end flow with a real WhatsApp Business account and CMS.
- Enable production monitoring and alerting.
- Perform smoke test: create ad → publish → verify on TV screen.

---

## Summary of Phases

| Phase | Name | Key Outputs |
|-------|------|-------------|
| 0 | Project Setup & Infrastructure Bootstrap | Repo, CI/CD, GCP services, GCS bucket, Cloud Tasks queue |
| 1 | Database Schema & ORM Foundation | SQLAlchemy models, Alembic migrations, CRUD helpers |
| 2 | WhatsApp Webhook Integration | FastAPI app, webhook endpoint, Cloud Tasks enqueue |
| 3 | Conversation Manager & Session State | Session loading/persistence, state machine |
| 4 | LLM Gateway & Intent Classification | Intent classifier, field extractor, guardrails, multi-language |
| 5 | Product Enrichment Pipeline | Barcode decoding, EAN lookup, web search enrichment |
| 6 | Media Storage (GCS) | MediaStore class, public URL strategy, TTL lifecycle |
| 7 | Ad Generation Engine (Nano Banana) | Async job model, polling + callback, regeneration modes |
| 8 | TV CMS Integration | CMSClient, publish/delete-all flows, audit logging |
| 9 | Admin Console | Admin API, authentication, browser UI |
| 10 | Hardening, Observability & Security | Error handling, logging, monitoring, security controls |
| 11 | End-to-End Testing & Launch Preparation | E2E tests, load tests, security review, production launch |
