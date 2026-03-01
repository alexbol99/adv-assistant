# adv-assistant

**WhatsApp Advertisement Assistant Bot** — a conversational bot that lets authorised store operators create, preview, and publish digital advertisements to in-store TV screens entirely through WhatsApp.

## Documentation

| Document | Description |
|----------|-------------|
| [Product Specification](docs/product-spec.md) | Goals, user roles, use cases, conversation experience, ad visual specs, publishing behaviour, and regional defaults. |
| [Architecture & Technical Specification](docs/architecture-and-technical-spec.md) | System components, data flow diagrams, conceptual data model, intents/commands, CMS integration interface, reliability, and prompt-injection guardrails. |
| [Technology Decisions](docs/technology-decisions.md) | Concrete technology decisions: Python stack, GCP Cloud Run + Cloud Tasks deployment, Cloud SQL PostgreSQL, GCS media storage, Nano Banana ad generation, and product enrichment approach for Israeli grocery. |
| [Workplan](docs/workplan.md) | Step-by-step implementation phases (0–11) for building the application. |

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
4. Run with Docker:
   - `docker compose up --build`

## CI / Staging Deploy

The repository includes a CI workflow (`.github/workflows/ci.yml`) that runs:

- lint (`ruff`)
- tests (`pytest`)
- Docker build
- optional staging deploy from `main` when these variables are configured:
  - repository variables: `GCP_PROJECT_ID`, `GCP_REGION`, `CLOUD_RUN_SERVICE`
  - repository secret: `GCP_SA_KEY`

## Infrastructure Bootstrap Helper

Use `/Users/alexanderbol/WebstormProjects/adv-assistant/scripts/bootstrap_gcp.sh` for initial GCP bootstrap (Cloud Run APIs, Cloud SQL, GCS bucket, Cloud Tasks queue).

## DB Access Provisioning (Staging/Production)

Use `/Users/alexanderbol/WebstormProjects/adv-assistant/scripts/provision_db_access.sh` to provision:
- databases: `adv_assistant_staging`, `adv_assistant_prod`
- service users (app + migrator per environment)
- Secret Manager password entries

Example:
- `CLOUD_SQL_INSTANCE=adv-assistant-pg /Users/alexanderbol/WebstormProjects/adv-assistant/scripts/provision_db_access.sh`

Defaults:
- `GCP_PROJECT_ID=ads-assistant-488908`
- existing user passwords are not rotated unless `ROTATE_EXISTING_PASSWORDS=true`

After the script runs, execute the printed SQL grant commands as Cloud SQL `postgres` superuser.
