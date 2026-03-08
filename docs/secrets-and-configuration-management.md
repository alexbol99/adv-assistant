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
  - required now: `STAGING_SECRET_DATABASE_URL`, `STAGING_SECRET_WHATSAPP_ACCESS_TOKEN`
  - optional now: `STAGING_SECRET_VERIFY_TOKEN`, `STAGING_SECRET_META_APP_SECRET`, `STAGING_SECRET_ADMIN_BASIC_USERNAME`, `STAGING_SECRET_ADMIN_BASIC_PASSWORD`, `STAGING_SECRET_OPENAI_API_KEY`, `STAGING_SECRET_GEMINI_API_KEY`, `STAGING_SECRET_NANA_BANANA_API_KEY`, `STAGING_SECRET_CMS_CITYSCREEN_APP_TOKEN`
- Production:
  - required now: `PROD_SECRET_DATABASE_URL`
  - other bindings are optional until production secret rollout is completed.

Example mapping:

- Repo variable: `STAGING_SECRET_WHATSAPP_ACCESS_TOKEN=WHATSAPP_ACCESS_TOKEN_STAGING`
- Cloud Run env after deploy: `WHATSAPP_ACCESS_TOKEN` injected from Secret Manager `WHATSAPP_ACCESS_TOKEN_STAGING:latest`

## Non-Secret Configuration

Keep non-secret values in environment-specific repository variables, for example:

- project/region/service names
- Cloud Tasks region/queue/service account
- deploy flags such as `STAGING_DEPLOY_ENABLED`

Do not store API tokens/passwords in repository variables.

## Verification Command

Use the helper script to verify both:

- Secret names exist in Secret Manager.
- Cloud Run worker/webhook services are bound to those secrets via env bindings.

Example (staging):

```bash
STAGING_SECRET_DATABASE_URL=DATABASE_URL_STAGING \
STAGING_SECRET_WHATSAPP_ACCESS_TOKEN=WHATSAPP_ACCESS_TOKEN_STAGING \
STAGING_SECRET_VERIFY_TOKEN=VERIFY_TOKEN_STAGING \
STAGING_SECRET_META_APP_SECRET=META_APP_SECRET_STAGING \
STAGING_SECRET_ADMIN_BASIC_USERNAME=ADMIN_BASIC_USERNAME_STAGING \
STAGING_SECRET_ADMIN_BASIC_PASSWORD=ADMIN_BASIC_PASSWORD_STAGING \
STAGING_SECRET_OPENAI_API_KEY=OPENAI_API_KEY_STAGING \
STAGING_SECRET_GEMINI_API_KEY=GEMINI_API_KEY_STAGING \
STAGING_SECRET_NANA_BANANA_API_KEY=NANA_BANANA_API_KEY_STAGING \
STAGING_SECRET_CMS_CITYSCREEN_APP_TOKEN=CMS_CITYSCREEN_APP_TOKEN_STAGING \
scripts/verify_cloudrun_secret_bindings.sh \
  --target staging \
  --project-id adv-assistant-staging-488908 \
  --region me-west1 \
  --worker-service adv-assistant-worker-staging \
  --webhook-service adv-assistant-webhook-staging
```
