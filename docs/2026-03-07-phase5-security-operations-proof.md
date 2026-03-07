# Phase 5 Security and Operations Proof (March 7, 2026)

This note captures evidence for the **Security and Operations** section in
`docs/phase5-compliance-checklist.md`.

## 1) Outbound request timeouts and retries are configured

- Open Food Facts client uses configurable timeout:
  - `ENRICHMENT_HTTP_TIMEOUT_SECONDS`
- Open Food Facts client now uses configurable retry policy:
  - `ENRICHMENT_MAX_ATTEMPTS`
  - `ENRICHMENT_RETRY_BASE_SECONDS`
- Retry behavior:
  - retries for `408`, `429`, and `5xx`
  - retries for request/network exceptions
  - non-retryable `4xx` fail fast

References:
- `src/adv_assistant/config.py`
- `src/adv_assistant/enrichment.py`
- `src/adv_assistant/main.py`
- `tests/test_phase5_enrichment.py`

## 2) Enrichment disable switch verified (`ENRICHMENT_ENABLED=false`)

- Settings load supports explicit disable (`false`).
- Service builder uses an empty provider chain when enrichment is disabled,
  preventing outbound enrichment provider calls.

References:
- `src/adv_assistant/config.py`
- `src/adv_assistant/main.py`
- `tests/test_config.py`
- `tests/test_main_enrichment.py`

## 3) API secrets in Secret Manager

Status: **pending environment-level verification**.

Code-level hardening completed:
- Removed hardcoded CMS token fallback from app config.

Still required outside code:
- Confirm staging/production secrets are provisioned in Secret Manager and
  bound into Cloud Run runtime.

