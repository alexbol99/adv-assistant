# Enterprise Readiness and Deployment Workplan

## Document Name Recommendation
Recommended name for this document: **Enterprise Readiness and Deployment Workplan**.

## Purpose
This document summarizes the current project state and defines a step-by-step plan to move `adv-assistant` from local/testing setup into an enterprise-grade staging and production model.

## Current State Summary
- App stack: FastAPI + async SQLAlchemy + Alembic + Postgres support.
- Runtime entry points: `GET/POST /webhook`, `POST /tasks/process-message`, `GET /health`.
- Async pattern exists: webhook verification + Cloud Tasks worker processing mode (`TASKS_MODE=cloud`).
- Local development currently runs with local env, optional ngrok exposure, and test WhatsApp number.
- CI exists for lint/test/docker build, with optional direct Cloud Run staging deploy from `main`.
- Infrastructure scripts already exist for GCP bootstrap and DB access provisioning.

## Target Enterprise Model (High-Level)
Per environment (`staging`, `production`) deploy separate resources:
- Cloud Run `webhook` service (public, Meta callback endpoint)
- Cloud Run `worker` service (private/authenticated, Cloud Tasks target)
- Cloud Tasks queue
- Cloud SQL PostgreSQL
- GCS media bucket
- Secret Manager secrets
- Environment-specific IAM service accounts

Recommended environment isolation:
- Separate GCP projects: `adv-assistant-staging`, `adv-assistant-prod`
- Separate Meta WhatsApp app/phone numbers for staging and production

## Branching, Release, and Promotion Strategy
- Development branches: `feature/*`, `codex/*`
- Integration branch: `main`
- Production release tags: `vMAJOR.MINOR.PATCH`
- Optional pre-release tags: `vMAJOR.MINOR.PATCH-rc.N`

Promotion model:
1. Merge to `main` triggers automatic staging deployment.
2. Validate staging.
3. Create release tag on validated commit.
4. Manual-approved production deployment uses the exact same image digest.

## Detailed Workplan

### Step 1: Foundation and Environment Isolation
1. Create dedicated GCP projects for staging and production.
2. Create separate billing labels and budgets/alerts per project.
3. Enable required APIs (`run`, `cloudtasks`, `sqladmin`, `storage`, `secretmanager`, `artifactregistry`, `monitoring`, `logging`).
4. Create separate WhatsApp Meta app/test number for staging and dedicated production number for production.

Exit criteria:
- Projects exist, API set enabled, budget alerts active, webhook URLs can be configured independently.

### Step 2: Artifact Registry and Immutable Images
1. Create Artifact Registry repo (Docker) in each project.
2. Build images in CI once per commit on `main`.
3. Push image and store immutable digest.
4. Deploy environments by digest only (not mutable tags).

Exit criteria:
- Staging and production both deploy from Artifact Registry digest references.

### Step 3: Split Webhook and Worker Services
1. Deploy two Cloud Run services per environment:
   - `adv-assistant-webhook-<env>`
   - `adv-assistant-worker-<env>`
2. Route Meta webhook only to webhook service.
3. Configure Cloud Tasks `TASKS_HANDLER_URL` to worker `/tasks/process-message`.
4. Keep worker unauthenticated access disabled.

Exit criteria:
- Webhook is low-latency and task enqueue-only; heavy processing isolated to worker.

### Step 4: Database Hardening and Migration Discipline
1. Use Cloud SQL Postgres per environment (preferred separate instances).
2. Create separate DBs and users for app/migrator (already supported by scripts).
3. Apply least-privilege grants (`app` read/write runtime, `migrator` schema change).
4. Execute Alembic migrations as dedicated migration job before app rollout.
5. Adopt backward-compatible migration policy (expand/contract).

Exit criteria:
- Migration and runtime credentials separated; rollback-safe migration process documented.

### Step 5: Secrets and Configuration Management
1. Move all secrets from `.env` style deployment to Secret Manager.
2. Inject secrets into Cloud Run via environment bindings.
3. Keep non-secret config in environment-specific config files/variables.
4. Add strict secret naming convention:
   - `WHATSAPP_ACCESS_TOKEN_STAGING`, `WHATSAPP_ACCESS_TOKEN_PROD`, etc.

Exit criteria:
- No production secrets in GitHub repo variables or local files; all in Secret Manager.

### Step 6: CI/CD Pipeline Upgrade
1. CI on PRs:
   - lint, format check, tests, image build validation.
2. CD on `main`:
   - build/push image
   - run staging migrations
   - deploy worker then webhook to staging
   - run smoke tests.
3. CD on release tag:
   - manual approval gate
   - run production migrations
   - deploy worker then webhook to production
   - post-deploy health checks.

Exit criteria:
- Staging auto deploy and production controlled promotion both operate from one pipeline.

### Step 7: Security Hardening (Pipeline and Runtime)
1. Replace `GCP_SA_KEY` JSON secret auth with GitHub OIDC Workload Identity Federation.
2. Restrict provider trust conditions by repo, branch, and tag patterns.
3. Use separate deploy service accounts for staging and production.
4. Restrict worker invocation to Cloud Tasks service account only.
5. Enforce least privilege on Cloud Run/Cloud SQL/GCS IAM roles.
6. For CMS outbound security, use a fixed egress IP and request CMS-side IP allowlisting (possible in this deployment model).
7. Add dependency scanning and container vulnerability scanning in CI.

Exit criteria:
- No long-lived service account keys in CI; short-lived federated credentials only.

### Step 8: Observability and Monitoring
1. Add structured logging with correlation IDs (`wamid`, `draft_id`, `job_id`).
2. Publish custom metrics from app events.
3. Build dashboards per environment.
4. Configure alerting policies.

Minimum alerts:
- Webhook 5xx rate spike
- Worker 5xx rate spike
- Queue oldest task age above threshold
- Task retry count anomaly
- DB CPU/connection saturation
- Ad generation failure ratio above threshold
- CMS publish failure ratio above threshold

Exit criteria:
- On-call can detect and diagnose issues from dashboards within minutes.

### Step 9: Scalability and Performance Readiness
1. Set explicit Cloud Run concurrency, max instances, and timeout values for both services.
2. Configure Cloud Tasks queue dispatch/retry limits based on provider SLAs.
3. Run load tests that simulate realistic inbound spikes.
4. Validate DB connection pool behavior under load.
5. Validate external API throttling behavior (Meta/OpenAI/Nano Banana/CMS).

Exit criteria:
- SLO targets validated in load test and queue behavior remains stable under peak load.

### Step 10: Cost and FinOps Controls
1. Track both variable and amortized unit economics.
2. Keep two primary unit metrics:
   - Cost per generation attempt
   - Cost per published ad
3. Keep one secondary UX metric:
   - Cost per advertisement dialog session
4. Add monthly cost review with anomaly detection and thresholds.

Definitions:
- Infra amortization per attempt = fixed monthly infra cost / monthly attempts
- Total cost per attempt = variable attempt cost + infra amortization per attempt

Example baseline (1,000 attempts/month):
- Estimated non-image-provider cost per attempt ≈ `0.0144439789 USD` (without Cloud Run/Tasks free-tier assumptions).
- Total per attempt = `0.0144439789 + image_provider_unit_price`.

Exit criteria:
- Monthly reporting shows trend for cost per attempt and cost per published ad.

### Step 11: Reliability, Rollback, and DR
1. Document rollback runbook: redeploy previous image digest.
2. Ensure migrations are rollback-aware (non-destructive first, destructive deferred).
3. Enable Cloud SQL backups and point-in-time recovery for production.
4. Define recovery objectives (RTO/RPO) and test restore procedure.

Exit criteria:
- Team can perform rollback and DB restore in controlled drill.

### Step 12: Production Readiness Gate
1. Security checklist complete.
2. Load test and failover test complete.
3. Monitoring/alerts and runbooks complete.
4. SLOs and cost baselines approved.
5. Go-live review signoff recorded.

Exit criteria:
- Formal approval to move production traffic.

## Recommended Metrics (What to Track)

### Product and Business Metrics
- `published_ad_success_count`
- `generation_attempt_count`
- `generation_to_publish_conversion_rate`
- `operator_active_daily_count`

### Reliability Metrics
- `webhook_ack_latency_p50/p95`
- `worker_task_duration_p50/p95/p99`
- `cloud_tasks_oldest_task_age`
- `cloud_tasks_retry_rate`
- `generation_success_rate`
- `cms_publish_success_rate`
- `dedup_skip_rate`

### Cost Metrics
- `cost_per_generation_attempt`
- `cost_per_published_ad`
- `infra_amortization_per_attempt`
- `llm_cost_per_attempt`
- `image_generation_cost_per_attempt`

## Monitoring and Alert Suggestions
- Critical (page): worker unavailable, queue age critical, DB saturated, publish failures sustained.
- High: generation failures above threshold for rolling 15 minutes.
- Medium: latency degradation, retry increase trend, unusual cost spike.
- Dashboard segmentation: staging vs production must be separate by default.

## Security Improvements Summary
- Adopt GitHub OIDC + Workload Identity Federation (replace static service-account JSON keys).
- Enforce least privilege everywhere (runtime SA, deploy SA, tasks invoker, DB users).
- Keep worker private and OIDC-protected.
- For third-party CMS integration, expose a stable outbound IP and ask the CMS team to allowlist that IP.
- Keep secrets in Secret Manager only.
- Add image/dependency scanning and secret scanning in CI.
- Add audit trail retention and access reviews.

## Suggested Execution Timeline
- Week 1-2: Steps 1-4
- Week 3: Steps 5-7
- Week 4: Steps 8-10
- Week 5: Steps 11-12 and production launch readiness

## Deliverables Checklist
- Environment isolation complete
- Artifact Registry digest-based deployments
- Split webhook/worker runtime
- Hardened DB and migration process
- OIDC-based CI authentication
- Dashboards + alerts + runbooks
- Unit-cost reporting
- Production readiness signoff
