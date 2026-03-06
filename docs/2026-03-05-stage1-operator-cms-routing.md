# Stage 1 Implementation Summary (March 5, 2026)

This document summarizes the implemented MVP Stage 1 scope: per-operator CMS routing and a basic admin mapping UI/API.

## Scope Delivered

1. Per-operator CMS mapping persisted in DB.
2. Publish routing uses operator mapping (campaign/playlist) per request.
3. Publish is blocked when operator is not mapped, with fixed Hebrew message.
4. Basic admin UI/API protected by HTTP Basic Auth.
5. Audit events for admin mapping actions and blocked publish attempts.

## Data Model and Migration

### New `operator` columns
- `meta_user_id` (nullable, unique, indexed)
- `cms_campaign_id` (nullable int)
- `cms_playlist_id` (nullable int)

### Migration
- `alembic/versions/20260305_0005_operator_cms_mapping.py`

### Startup schema guard
- App startup validates the new `operator` columns and fails fast with a migration instruction when missing.

## Repository Changes

In `OperatorRepository`:
- `create(...)` now accepts optional mapping fields.
- Added `get_by_meta_user_id(...)`.
- Added `update_cms_mapping(...)`.

## Publish Flow Changes

### Routing behavior
- `publish_generated_image(...)` accepts per-call `campaign_id` / `playlist_id` override.
- Pipeline passes operator mapping values to CMS publisher on confirm-publish.

### Block behavior (missing mapping)
- Bot response:
  - "אתה לא מחובר למערכת כרגע, פנה לתמיכה כדי לייצר את החיבור"
- No CMS side effects.
- Audit event emitted: `publish_blocked_operator_not_connected`.

## Admin API/UI

### Authentication
- Environment variables:
  - `ADMIN_BASIC_USERNAME`
  - `ADMIN_BASIC_PASSWORD`
- If missing, admin endpoints return `503`.

### Endpoints
- `GET /admin` – minimal mapping form UI.
- `POST /admin/operators/connect` – create/update mapping.
- `GET /admin/operators/by-phone/{phone}` – fetch mapping.
- `GET /admin/operators/by-meta/{meta_user_id}` – fetch mapping.

### Behavior
- Supports lookup by phone and/or existing Meta user ID.
- Can create operator when phone is provided and operator does not exist.
- Lookup responses now include operator profile context used in generation:
  - `display_name`, `language`, `currency`
  - `business_name`, `logo_url`, `brand_colors`
  - `store_type`, `creative_guidance`
  - `created_at`, `updated_at`
- Admin page now includes a dedicated "Operator Profile Lookup" panel (phone/meta lookup buttons + JSON output).
- Emits admin audit events:
  - `admin_operator_created_with_cms_mapping`
  - `admin_operator_cms_mapping_updated`

## Test Coverage Added/Updated

- Migration tests:
  - `tests/test_migrations.py`
- Repository tests:
  - `tests/test_repositories.py`
- CMS publish integration tests:
  - `tests/test_phase8_cms_publish.py`
- Admin tests:
  - `tests/test_admin_mvp.py`

## Local Run Notes

1. Run migrations:
   - `uv run alembic upgrade head`
2. Start app with env loading:
   - `make run`
3. Open admin page:
   - `/admin`
