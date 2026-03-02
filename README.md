# adv-assistant

**WhatsApp Advertisement Assistant Bot** — a conversational bot that lets authorised store operators create, preview, and publish digital advertisements to in-store TV screens entirely through WhatsApp.

## Documentation

| Document | Description |
|----------|-------------|
| [Product Specification](docs/product-spec.md) | Goals, user roles, use cases, conversation experience, ad visual specs, publishing behaviour, and regional defaults. |
| [Architecture & Technical Specification](docs/architecture-and-technical-spec.md) | System components, data flow diagrams, conceptual data model, intents/commands, CMS integration interface, reliability, and prompt-injection guardrails. |
| [Technology Decisions](docs/technology-decisions.md) | Concrete technology decisions: Python stack, GCP Cloud Run + Cloud Tasks deployment, Cloud SQL PostgreSQL, GCS media storage, Nano Banana ad generation, and product enrichment approach for Israeli grocery. |
| [Workplan](docs/workplan.md) | Step-by-step implementation phases (0–11) for building the application. |
| [Phase 5 Compliance Checklist](docs/phase5-compliance-checklist.md) | Pre-production legal/compliance checklist for enrichment sources and data handling. |

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
   - `uv run uvicorn adv_assistant.main:app --reload --host 0.0.0.0 --port 8080`
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
  - provider chain order: Open Food Facts -> EAN fallback -> web-search fallback
  - only normalized enrichment fields are stored in DB; raw provider payloads are not persisted
- Ad generation configuration (Phase 7, polling-only):
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

## Database Migrations

- Upgrade to latest schema:
  - `uv run alembic upgrade head`
  - `make migrate` (recommended for local; auto-loads `.env` / `DATABASE_URL`)
  - required after pulling schema changes (for example Phase 5 enrichment columns)
- Create a new migration revision:
  - `uv run alembic revision -m "your message"`
- Override migration DB URL:
  - `ALEMBIC_DATABASE_URL=postgresql+psycopg://... uv run alembic upgrade head`

## CI / Staging Deploy

The repository includes a CI workflow (`.github/workflows/ci.yml`) that runs:

- lint (`ruff`)
- tests (`pytest`)
- Docker build
- optional staging deploy from `main` when these variables are configured:
  - repository variables: `GCP_PROJECT_ID`, `GCP_REGION`, `CLOUD_RUN_SERVICE`
  - repository secret: `GCP_SA_KEY`

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

Example:
- `CLOUD_SQL_INSTANCE=adv-assistant-pg scripts/provision_db_access.sh`

Recommended first run (bootstraps postgres admin password into Secret Manager and applies grants automatically):
- `BOOTSTRAP_POSTGRES_ADMIN_PASSWORD=true CLOUD_SQL_INSTANCE=adv-assistant-pg scripts/provision_db_access.sh`

Defaults:
- `GCP_PROJECT_ID=ads-assistant-488908`
- existing user passwords are not rotated unless `ROTATE_EXISTING_PASSWORDS=true`
- automatic SQL grant application is enabled (`APPLY_SQL_GRANTS=true`)

If automatic grant application is unavailable (missing `psql`, `cloud-sql-proxy`, or admin password), the script prints manual SQL commands.
