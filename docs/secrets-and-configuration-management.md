# Secrets and Configuration Management (Step 5)

This document defines how runtime secrets and non-secret configuration are managed for staging and production.

## Principles

- Store sensitive values in Secret Manager.
- Bind secrets to Cloud Run environment variables during deploy (`--update-secrets`).
- Keep non-secret runtime configuration in environment-specific repository variables.
- Use strict secret naming by environment suffix (`_STAGING`, `_PROD`).

## Secret Naming Convention

Use uppercase, environment-suffixed secret names:

- `WHATSAPP_ACCESS_TOKEN_STAGING`
- `VERIFY_TOKEN_STAGING`
- `META_APP_SECRET_STAGING`
- `OPENAI_API_KEY_STAGING`
- `GEMINI_API_KEY_STAGING`
- `NANA_BANANA_API_KEY_STAGING`
- `CMS_CITYSCREEN_APP_TOKEN_STAGING`
- `DATABASE_URL_STAGING`

Production follows the same suffix pattern (`_PROD`).

Current rollout decision:

- Use the current WhatsApp token value for `WHATSAPP_ACCESS_TOKEN_STAGING`.
- Skip `WHATSAPP_ACCESS_TOKEN_PROD` for now; add it later before production enablement.

## Cloud Run Secret Bindings

CI deploy jobs support secret bindings via repository variables containing secret names:

- Staging:
  - required now: `STAGING_SECRET_WHATSAPP_ACCESS_TOKEN`
  - optional now: `STAGING_SECRET_DATABASE_URL`, `STAGING_SECRET_VERIFY_TOKEN`, `STAGING_SECRET_META_APP_SECRET`, `STAGING_SECRET_ADMIN_BASIC_USERNAME`, `STAGING_SECRET_ADMIN_BASIC_PASSWORD`, `STAGING_SECRET_OPENAI_API_KEY`, `STAGING_SECRET_GEMINI_API_KEY`, `STAGING_SECRET_NANA_BANANA_API_KEY`, `STAGING_SECRET_CMS_CITYSCREEN_APP_TOKEN`
- Production:
  - all bindings are optional until production secret rollout is completed.

Example mapping:

- Repo variable: `STAGING_SECRET_WHATSAPP_ACCESS_TOKEN=WHATSAPP_ACCESS_TOKEN_STAGING`
- Cloud Run env after deploy: `WHATSAPP_ACCESS_TOKEN` injected from Secret Manager `WHATSAPP_ACCESS_TOKEN_STAGING:latest`

## Non-Secret Configuration

Keep non-secret values in environment-specific repository variables, for example:

- project/region/service names
- Cloud Tasks region/queue/service account
- deploy flags such as `STAGING_DEPLOY_ENABLED`

Do not store API tokens/passwords in repository variables.

