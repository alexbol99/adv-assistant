# Database Migration Discipline (Expand/Contract)

This document defines the migration policy for staging/production deployments.

## Goals

- Keep runtime DB access (`app` user) separate from schema change access (`migrator` user).
- Ensure migrations run before app rollout in each environment.
- Keep releases rollback-safe by using backward-compatible schema evolution.

## Environment Model

- `staging` and `production` run in separate GCP projects.
- Each environment should use its own Cloud SQL Postgres instance (preferred).
- Each environment has:
  - one runtime DB user (`app`) for normal read/write operations
  - one migrator DB user (`migrator`) for Alembic schema changes

## Least-Privilege DB Roles

Use `scripts/provision_db_access.sh` with the environment target:

- `PROVISION_TARGETS=staging` for staging project/instance
- `PROVISION_TARGETS=production` for production project/instance

Privileges are intentionally split:

- `app` user:
  - `CONNECT` to DB
  - `USAGE` on schema
  - `SELECT/INSERT/UPDATE/DELETE` on tables
  - no schema `CREATE`
- `migrator` user:
  - `CONNECT` to DB
  - schema `USAGE` + `CREATE`
  - table/sequence privileges required by Alembic migrations
  - no role/database creation privileges

## Deployment Sequence

For each environment deployment:

1. Build and publish immutable image.
2. Run dedicated migration job with migrator credentials.
3. Only after migration success, deploy worker service.
4. Deploy webhook service last.

CI enforces this order in `.github/workflows/ci.yml`.

## Expand/Contract Policy

Always split destructive schema changes into at least two releases.

### Expand release (backward-compatible)

- Add new nullable columns/tables/indexes.
- Backfill data asynchronously where needed.
- Keep old and new schema paths supported by application code.

### Contract release (destructive)

- Remove old columns/tables/indexes only after application code no longer reads/writes them.
- Execute destructive migration in a later release window.

## Practical Rules

- Never combine "drop old schema" with "code switch to new schema" in the same release.
- Prefer additive migrations first; delay `drop_*` operations.
- Validate migration on staging before production tag promotion.
- Keep Alembic downgrade paths for recent revisions where practical, but rely on forward-fix as default production strategy.

