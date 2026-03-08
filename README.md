# adv-assistant

**WhatsApp Advertisement Assistant Bot** — a conversational bot that lets authorised store operators create, preview, and publish digital advertisements to in-store TV screens entirely through WhatsApp.

## Documentation

| Document | Description |
|----------|-------------|
| [Product Specification](docs/product-spec.md) | Goals, user roles, use cases, conversation experience, ad visual specs, publishing behaviour, and regional defaults. |
| [Architecture & Technical Specification](docs/architecture-and-technical-spec.md) | System components, data flow diagrams, conceptual data model, intents/commands, CMS integration interface, reliability, and prompt-injection guardrails. |
| [Technology Decisions](docs/technology-decisions.md) | Concrete technology decisions: Python stack, GCP Cloud Run + Cloud Tasks deployment, Cloud SQL PostgreSQL, GCS media storage, Nano Banana ad generation, and product enrichment approach for Israeli grocery. |
| [Workplan](docs/workplan.md) | Step-by-step implementation phases (0–11) for building the application. |
| [MVP Priority Plan](docs/mvp-priority-plan-2026-03-05.md) | Current MVP stage order, locked decisions, and acceptance tests. |
| [Phase 5 Compliance Checklist](docs/phase5-compliance-checklist.md) | Pre-production legal/compliance checklist for enrichment sources and data handling. |
| [DB Migration Discipline](docs/database-migration-discipline.md) | Expand/contract migration policy and rollout sequence for safe staged deployments. |
| [Secrets and Configuration Management](docs/secrets-and-configuration-management.md) | Secret Manager naming convention and Cloud Run secret-binding rollout. |

## Quick Summary

- **WhatsApp provider**: Meta Cloud API
- **Operator model**: multiple authorised phone numbers, same permissions
- **Output**: 1920 × 1080 px landscape, 10% safe margins
- **CMS publishing**: append-only; delete-all is the only removal operation; no scheduling
- **Region / currency**: Israel / ILS (₪) by default

## Development Setup

1. Install dependencies:
   - `uv sync --all-groups`
2. Run quality checks:
   - `uv run ruff check .`
   - `uv run ruff format --check .`
   - `uv run pytest`
3. Run the app locally:
   - `uv run uvicorn --app-dir src adv_assistant.main:app --reload --host 0.0.0.0 --port 8080`
   - or `make run` (auto-loads `.env` into process env)
4. Run with Docker:
   - `docker compose up --build`
   - create `.env` from `.env.example` and set a local-only `POSTGRES_PASSWORD`
   - app container loads `.env` via `env_file`
   - never reuse local `.env` credentials in staging/production

## Webhook + Task Pipeline (Phase 2)

- Required webhook env vars:
  - `VERIFY_TOKEN`
  - `META_APP_SECRET`
- WhatsApp send API env vars (for unauthorized rejection message delivery):
  - `WHATSAPP_ACCESS_TOKEN`
  - `PHONE_NUMBER_ID`
  - optional `GRAPH_API_VERSION` (default `v21.0`)
- Runtime modes:
  - `TASKS_MODE=inline` for local development (task payload is processed in-process)
  - `TASKS_MODE=cloud` for Cloud Tasks enqueue
- Admin mapping UI auth:
  - `ADMIN_BASIC_USERNAME`
  - `ADMIN_BASIC_PASSWORD`
  - if these are missing, `/admin` and admin API endpoints return `503 Admin authentication is not configured`
  - local dev tip: run via `make run` so `.env` is loaded into process environment
- Cloud mode requires:
  - `GCP_PROJECT_ID`, `TASKS_REGION`, `TASKS_QUEUE`
  - `TASKS_HANDLER_URL`
  - `TASKS_SERVICE_ACCOUNT_EMAIL`
  - `TASKS_OIDC_AUDIENCE` (optional, defaults to `TASKS_HANDLER_URL`)
  - `TASKS_ALLOWED_SERVICE_ACCOUNT_EMAIL` (recommended)
- Security controls:
  - Webhook POST must pass `X-Hub-Signature-256` validation
  - Replay window is configurable via `REPLAY_WINDOW_SECONDS` (default `300`)
  - Unauthorized numbers receive one rejection message per `UNAUTHORIZED_REJECTION_WINDOW_MINUTES` (default `60`)
  - `POST /tasks/process-message` accepts only valid OIDC bearer tokens
- LLM configuration (Phase 4):
  - `OPENAI_API_KEY`
  - optional `OPENAI_BASE_URL`
  - `LLM_CLASSIFICATION_MODEL`, `LLM_EXTRACTION_MODEL`, `LLM_REPLY_MODEL`
  - `LLM_MAX_RETRIES` (schema mismatch retry count, default `1`)
  - `LLM_TIMEOUT_SECONDS` (default `15`)
  - `LLM_MAX_INPUT_CHARS` (default `2000`)
  - confirmation button payloads are deterministic and bypass LLM:
    - `confirm_publish`, `confirm_delete_all`, `cancel_delete_all`
- Enrichment configuration (Phase 5):
  - `ENRICHMENT_ENABLED` (default `true`)
  - `OPEN_FOOD_FACTS_BASE_URL` (default `https://world.openfoodfacts.org`)
  - `ENRICHMENT_HTTP_TIMEOUT_SECONDS` (default `8`)
  - `ENRICHMENT_MAX_ATTEMPTS` (default `2`)
  - `ENRICHMENT_RETRY_BASE_SECONDS` (default `0.5`)
  - provider chain order: Open Food Facts -> EAN fallback -> web-search fallback
  - only normalized enrichment fields are stored in DB; raw provider payloads are not persisted
- Ad generation configuration (Phase 7):
  - Gemini (preferred):
    - `GEMINI_API_KEY`
    - `GEMINI_MODEL` (default `gemini-3.1-flash-image-preview`)
    - `GEMINI_BASE_URL` (default `https://generativelanguage.googleapis.com/v1beta`)
    - `GEMINI_TIMEOUT_SECONDS` (default `30`)
  - Nano Banana (legacy fallback):
    - `NANA_BANANA_API_KEY`
    - `NANA_BANANA_BASE_URL` (quickstart style, e.g. `https://api.nanobananaapi.ai/api/v1/nanobanana`)
    - `NANA_BANANA_API_URL` (optional explicit override for generate endpoint)
    - `NANA_BANANA_STATUS_API_URL_TEMPLATE` (optional explicit status endpoint template; must include `{job_id}`)
    - `NANA_BANANA_MODEL` (default `nanobanana-2`)
    - `NANA_BANANA_GENERATION_TYPE` (default `TEXTTOIAMGE`, per provider quickstart)
    - `NANA_BANANA_NUM_IMAGES` (default `1`)
    - `NANA_BANANA_WATERMARK` (optional boolean)
    - `NANA_BANANA_TIMEOUT_SECONDS` (default `20`)
    - `NANA_BANANA_POLL_INITIAL_SECONDS` (default `2`)
    - `NANA_BANANA_POLL_MAX_SECONDS` (default `10`)
    - `NANA_BANANA_POLL_TIMEOUT_SECONDS` (default `900`)
  - `AD_RENDER_WIDTH` / `AD_RENDER_HEIGHT` (default `1920x1080`; aspect ratio derived automatically)
  - when using Gemini image generation, set `MEDIA_STORE_MODE` to a cloud backend (`gcs` or `s3`) for public preview URLs
- Media lifecycle/storage configuration (Phase 6):
  - `MEDIA_STORE_MODE` (`noop`, `gcs`, or `s3`, default `noop`)
  - `MEDIA_GCS_BUCKET` (required when `MEDIA_STORE_MODE=gcs`)
  - `MEDIA_GCS_PUBLIC_BASE_URL` (default `https://storage.googleapis.com`)
  - `MEDIA_GCS_OBJECT_PREFIX` (default `operator-photos`)
  - `MEDIA_S3_BUCKET` (required when `MEDIA_STORE_MODE=s3`)
  - `MEDIA_S3_REGION` (recommended for `MEDIA_STORE_MODE=s3`)
  - `MEDIA_S3_PUBLIC_BASE_URL` (optional full public prefix, for example a CloudFront URL)
  - `MEDIA_S3_OBJECT_PREFIX` (default `operator-photos`)
  - `MEDIA_S3_ENDPOINT_URL` (optional, for S3-compatible endpoints)
  - `MEDIA_LIFECYCLE_DAYS` (default `90`)
  - `MEDIA_VERIFY_LIFECYCLE_ON_STARTUP` (default `false`; when `true`, app startup validates a matching GCS Delete lifecycle rule)
  - `WHATSAPP_MEDIA_TIMEOUT_SECONDS` (default `15`)
  - authorized operator image messages now trigger Meta media download + upload to configured media store and update draft `photo_url`

## Database Migrations

- Upgrade to latest schema:
  - `uv run alembic upgrade head`
  - `make migrate` (recommended for local; auto-loads `.env` / `DATABASE_URL`)
  - required after pulling schema changes (for example Phase 5 enrichment columns)
- Create a new migration revision:
  - `uv run alembic revision -m "your message"`
- Override migration DB URL:
  - `ALEMBIC_DATABASE_URL=postgresql+psycopg://... uv run alembic upgrade head`

## CI / Staging + Production Deploy

The repository includes a CI workflow (`.github/workflows/ci.yml`) that runs:

- lint (`ruff`)
- tests (`pytest`)
- Docker build validation on PRs and non-`main` branch pushes
- on `main`, one Docker image build is pushed to Artifact Registry in both staging and prod projects
- on release tags (`v*`), one Docker image build is pushed to Artifact Registry in both staging and prod projects
- image digest is resolved and saved as CI artifact (`image-digests`)
- staging migration job from `main` runs Alembic with a dedicated migrator DB user and must succeed before staging deploy
- staging deploy from `main` uses immutable image digest (`--image ...@sha256:...`), injects configured Secret Manager bindings, and deploys worker first, then webhook
- production migration job from release tag (`v*`) runs Alembic with a dedicated migrator DB user and must succeed before production deploy
- production deploy from release tag (`v*`) uses immutable image digest (`--image ...@sha256:...`), injects configured Secret Manager bindings, and deploys worker first, then webhook

Required repository variables for `main` image publish:
- `ARTIFACT_REGISTRY_REGION` (for example `me-west1`)
- `ARTIFACT_REGISTRY_REPOSITORY` (for example `adv-assistant`)
- `STAGING_GCP_PROJECT_ID`
- `PROD_GCP_PROJECT_ID`

Required repository variables for staging deploy:
- `STAGING_DEPLOY_ENABLED=true` (set to `false` to skip staging deploy while infra is not ready)
- `STAGING_GCP_REGION`
- `STAGING_CLOUD_RUN_WORKER_SERVICE`
- `STAGING_CLOUD_RUN_WEBHOOK_SERVICE`
- `STAGING_TASKS_REGION`
- `STAGING_TASKS_QUEUE`
- `STAGING_TASKS_SERVICE_ACCOUNT_EMAIL`
- `STAGING_SECRET_DATABASE_URL` (Secret Manager mapping for `DATABASE_URL`; prevents SQLite fallback in Cloud Run)
- `STAGING_SECRET_WHATSAPP_ACCESS_TOKEN` (Step 5 convention; currently `WHATSAPP_ACCESS_TOKEN_STAGING`)
- optional: `STAGING_CLOUD_RUN_ALLOW_UNAUTHENTICATED=true` (applies to webhook service only)

Optional repository variables for staging secret bindings:
- `STAGING_SECRET_VERIFY_TOKEN`
- `STAGING_SECRET_META_APP_SECRET`
- `STAGING_SECRET_ADMIN_BASIC_USERNAME`
- `STAGING_SECRET_ADMIN_BASIC_PASSWORD`
- `STAGING_SECRET_OPENAI_API_KEY`
- `STAGING_SECRET_GEMINI_API_KEY`
- `STAGING_SECRET_NANA_BANANA_API_KEY`
- `STAGING_SECRET_CMS_CITYSCREEN_APP_TOKEN`

Required repository variables for staging migration job:
- `STAGING_CLOUD_SQL_CONNECTION_NAME` (format: `project:region:instance`)
- `STAGING_DB_NAME`
- `STAGING_DB_MIGRATOR_USER`
- `STAGING_DB_MIGRATOR_PASS_SECRET` (Secret Manager secret name in staging project)

Required repository variables for production deploy:
- `PROD_GCP_REGION`
- `PROD_CLOUD_RUN_WORKER_SERVICE`
- `PROD_CLOUD_RUN_WEBHOOK_SERVICE`
- `PROD_TASKS_REGION`
- `PROD_TASKS_QUEUE`
- `PROD_TASKS_SERVICE_ACCOUNT_EMAIL`
- `PROD_SECRET_DATABASE_URL` (Secret Manager mapping for `DATABASE_URL`; prevents SQLite fallback in Cloud Run)
- optional: `PROD_CLOUD_RUN_ALLOW_UNAUTHENTICATED=true` (applies to webhook service only)

Optional repository variables for production secret bindings:
- `PROD_SECRET_VERIFY_TOKEN`
- `PROD_SECRET_META_APP_SECRET`
- `PROD_SECRET_WHATSAPP_ACCESS_TOKEN` (deferred for now)
- `PROD_SECRET_ADMIN_BASIC_USERNAME`
- `PROD_SECRET_ADMIN_BASIC_PASSWORD`
- `PROD_SECRET_OPENAI_API_KEY`
- `PROD_SECRET_GEMINI_API_KEY`
- `PROD_SECRET_NANA_BANANA_API_KEY`
- `PROD_SECRET_CMS_CITYSCREEN_APP_TOKEN`

Required repository variables for production migration job:
- `PROD_CLOUD_SQL_CONNECTION_NAME` (format: `project:region:instance`)
- `PROD_DB_NAME`
- `PROD_DB_MIGRATOR_USER`
- `PROD_DB_MIGRATOR_PASS_SECRET` (Secret Manager secret name in production project)

Required repository secret:
- `GCP_SA_KEY`

## Infrastructure Bootstrap Helper

Use `scripts/bootstrap_gcp.sh` for initial GCP bootstrap (Cloud Run APIs, Cloud SQL, GCS bucket, Cloud Tasks queue).

Note: Cloud Tasks may not be available in the same region as Cloud Run/Cloud SQL.  
Set `TASKS_REGION` explicitly (for this project, use `me-central1`).

## DB Access Provisioning (Staging/Production)

Use `scripts/provision_db_access.sh` to provision:
- databases: `adv_assistant_staging`, `adv_assistant_prod`
- service users (app + migrator per environment)
- Secret Manager password entries
- SQL grants and least-privilege hardening (automatic when admin DB password is available)

Example (staging-only target in staging project/instance):
- `GCP_PROJECT_ID=adv-assistant-staging-488908 CLOUD_SQL_INSTANCE=adv-assistant-staging-pg PROVISION_TARGETS=staging scripts/provision_db_access.sh`

Example (production-only target in production project/instance):
- `GCP_PROJECT_ID=adv-assistant-prod-488908 CLOUD_SQL_INSTANCE=adv-assistant-prod-pg PROVISION_TARGETS=production scripts/provision_db_access.sh`

Recommended first run (bootstraps postgres admin password into Secret Manager and applies grants automatically):
- `BOOTSTRAP_POSTGRES_ADMIN_PASSWORD=true GCP_PROJECT_ID=adv-assistant-staging-488908 CLOUD_SQL_INSTANCE=adv-assistant-staging-pg PROVISION_TARGETS=staging scripts/provision_db_access.sh`

Defaults:
- `PROVISION_TARGETS=staging,production` (set explicitly to `staging` or `production` for isolated projects/instances)
- existing user passwords are not rotated unless `ROTATE_EXISTING_PASSWORDS=true`
- automatic SQL grant application is enabled (`APPLY_SQL_GRANTS=true`)

If automatic grant application is unavailable (missing `psql`, `cloud-sql-proxy`, or admin password), the script prints manual SQL commands.

## Secret Binding Verification (Staging/Production)

Use `scripts/verify_cloudrun_secret_bindings.sh` to verify:

- configured secret names exist in Secret Manager;
- Cloud Run `worker` and `webhook` services are bound to the expected secrets.

Example (staging):

- `STAGING_SECRET_DATABASE_URL=DATABASE_URL_STAGING STAGING_SECRET_WHATSAPP_ACCESS_TOKEN=WHATSAPP_ACCESS_TOKEN_STAGING scripts/verify_cloudrun_secret_bindings.sh --target staging --project-id adv-assistant-staging-488908 --region me-west1 --worker-service adv-assistant-worker-staging --webhook-service adv-assistant-webhook-staging`

Run migrations explicitly (same approach as CI migration jobs):
- `GCP_PROJECT_ID=adv-assistant-staging-488908 CLOUD_SQL_CONNECTION_NAME=adv-assistant-staging-488908:me-west1:adv-assistant-staging-pg DB_NAME=adv_assistant_staging DB_MIGRATOR_USER=adv_assistant_migrator_staging DB_MIGRATOR_PASS_SECRET=DB_MIGRATOR_PASS_STAGING scripts/run_cloudsql_migration.sh`
