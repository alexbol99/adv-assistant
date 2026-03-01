#!/usr/bin/env bash
set -euo pipefail

# Provision Cloud SQL databases, service users, and Secret Manager entries
# for staging/production runtime and migration access.
#
# Defaults are tuned for this project and can be overridden via env vars.
# The script is idempotent and safe by default:
# - existing users are not rotated unless ROTATE_EXISTING_PASSWORDS=true
# - secrets are created/updated only when passwords are created/rotated
#
# Required:
#   CLOUD_SQL_INSTANCE
#
# Optional (defaults shown):
#   GCP_PROJECT_ID=ads-assistant-488908
#   DB_STAGING=adv_assistant_staging
#   DB_PROD=adv_assistant_prod
#   APP_USER_STAGING=adv_assistant_app_staging
#   APP_USER_PROD=adv_assistant_app_prod
#   MIGRATOR_USER_STAGING=adv_assistant_migrator_staging
#   MIGRATOR_USER_PROD=adv_assistant_migrator_prod
#   ROTATE_EXISTING_PASSWORDS=false

GCP_PROJECT_ID="${GCP_PROJECT_ID:-ads-assistant-488908}"
CLOUD_SQL_INSTANCE="${CLOUD_SQL_INSTANCE:-}"

DB_STAGING="${DB_STAGING:-adv_assistant_staging}"
DB_PROD="${DB_PROD:-adv_assistant_prod}"

APP_USER_STAGING="${APP_USER_STAGING:-adv_assistant_app_staging}"
APP_USER_PROD="${APP_USER_PROD:-adv_assistant_app_prod}"
MIGRATOR_USER_STAGING="${MIGRATOR_USER_STAGING:-adv_assistant_migrator_staging}"
MIGRATOR_USER_PROD="${MIGRATOR_USER_PROD:-adv_assistant_migrator_prod}"

APP_PASS_SECRET_STAGING="${APP_PASS_SECRET_STAGING:-DB_APP_PASS_STAGING}"
APP_PASS_SECRET_PROD="${APP_PASS_SECRET_PROD:-DB_APP_PASS_PROD}"
MIGRATOR_PASS_SECRET_STAGING="${MIGRATOR_PASS_SECRET_STAGING:-DB_MIGRATOR_PASS_STAGING}"
MIGRATOR_PASS_SECRET_PROD="${MIGRATOR_PASS_SECRET_PROD:-DB_MIGRATOR_PASS_PROD}"

ROTATE_EXISTING_PASSWORDS="${ROTATE_EXISTING_PASSWORDS:-false}"

if [[ -z "$CLOUD_SQL_INSTANCE" ]]; then
  echo "CLOUD_SQL_INSTANCE is required."
  echo "Example: CLOUD_SQL_INSTANCE=adv-assistant-pg scripts/provision_db_access.sh"
  exit 1
fi

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd gcloud
require_cmd openssl

echo "Using GCP project: $GCP_PROJECT_ID"
echo "Using Cloud SQL instance: $CLOUD_SQL_INSTANCE"

gcloud services enable sqladmin.googleapis.com secretmanager.googleapis.com \
  --project "$GCP_PROJECT_ID" >/dev/null

database_exists() {
  local db="$1"
  gcloud sql databases list \
    --instance "$CLOUD_SQL_INSTANCE" \
    --project "$GCP_PROJECT_ID" \
    --format="value(name)" | grep -Fxq "$db"
}

create_database_if_missing() {
  local db="$1"
  if database_exists "$db"; then
    echo "Database exists: $db"
  else
    echo "Creating database: $db"
    gcloud sql databases create "$db" \
      --instance "$CLOUD_SQL_INSTANCE" \
      --project "$GCP_PROJECT_ID" >/dev/null
  fi
}

user_exists() {
  local user="$1"
  gcloud sql users list \
    --instance "$CLOUD_SQL_INSTANCE" \
    --project "$GCP_PROJECT_ID" \
    --format="value(name)" | grep -Fxq "$user"
}

secret_exists() {
  local secret="$1"
  gcloud secrets describe "$secret" --project "$GCP_PROJECT_ID" >/dev/null 2>&1
}

upsert_secret_value() {
  local secret="$1"
  local value="$2"

  if secret_exists "$secret"; then
    printf "%s" "$value" | gcloud secrets versions add "$secret" \
      --data-file=- \
      --project "$GCP_PROJECT_ID" >/dev/null
    echo "Added secret version: $secret"
  else
    printf "%s" "$value" | gcloud secrets create "$secret" \
      --data-file=- \
      --project "$GCP_PROJECT_ID" >/dev/null
    echo "Created secret: $secret"
  fi
}

ensure_user_and_secret() {
  local user="$1"
  local secret="$2"
  local pass_var_name="$3"

  local password
  password="$(openssl rand -hex 24)"

  if user_exists "$user"; then
    echo "User exists: $user"
    if [[ "$ROTATE_EXISTING_PASSWORDS" == "true" ]]; then
      echo "Rotating password for user: $user"
      gcloud sql users set-password "$user" \
        --instance "$CLOUD_SQL_INSTANCE" \
        --password "$password" \
        --project "$GCP_PROJECT_ID" >/dev/null
      upsert_secret_value "$secret" "$password"
      printf -v "$pass_var_name" "%s" "$password"
    else
      echo "Skipping password rotation for existing user: $user"
      if ! secret_exists "$secret"; then
        echo "WARNING: secret $secret is missing. Re-run with ROTATE_EXISTING_PASSWORDS=true to recreate."
      fi
    fi
  else
    echo "Creating user: $user"
    gcloud sql users create "$user" \
      --instance "$CLOUD_SQL_INSTANCE" \
      --password "$password" \
      --project "$GCP_PROJECT_ID" >/dev/null
    upsert_secret_value "$secret" "$password"
    printf -v "$pass_var_name" "%s" "$password"
  fi
}

create_database_if_missing "$DB_STAGING"
create_database_if_missing "$DB_PROD"

APP_PASS_STAGING=""
APP_PASS_PROD=""
MIGRATOR_PASS_STAGING=""
MIGRATOR_PASS_PROD=""

ensure_user_and_secret "$APP_USER_STAGING" "$APP_PASS_SECRET_STAGING" APP_PASS_STAGING
ensure_user_and_secret "$APP_USER_PROD" "$APP_PASS_SECRET_PROD" APP_PASS_PROD
ensure_user_and_secret "$MIGRATOR_USER_STAGING" "$MIGRATOR_PASS_SECRET_STAGING" MIGRATOR_PASS_STAGING
ensure_user_and_secret "$MIGRATOR_USER_PROD" "$MIGRATOR_PASS_SECRET_PROD" MIGRATOR_PASS_PROD

CONNECTION_NAME="$(gcloud sql instances describe "$CLOUD_SQL_INSTANCE" \
  --project "$GCP_PROJECT_ID" \
  --format='value(connectionName)')"

echo
echo "Provisioning complete."
echo
echo "Next step: run SQL grants as postgres superuser."
echo "Staging grants:"
cat <<EOF
gcloud sql connect "$CLOUD_SQL_INSTANCE" --project "$GCP_PROJECT_ID" --user=postgres --database="$DB_STAGING" <<'SQL'
GRANT CONNECT ON DATABASE $DB_STAGING TO $APP_USER_STAGING;
GRANT CONNECT ON DATABASE $DB_STAGING TO $MIGRATOR_USER_STAGING;

GRANT USAGE ON SCHEMA public TO $APP_USER_STAGING;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO $APP_USER_STAGING;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO $APP_USER_STAGING;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO $APP_USER_STAGING;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO $APP_USER_STAGING;

GRANT USAGE, CREATE ON SCHEMA public TO $MIGRATOR_USER_STAGING;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON ALL TABLES IN SCHEMA public TO $MIGRATOR_USER_STAGING;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO $MIGRATOR_USER_STAGING;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLES TO $MIGRATOR_USER_STAGING;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO $MIGRATOR_USER_STAGING;
SQL
EOF

echo
echo "Production grants:"
cat <<EOF
gcloud sql connect "$CLOUD_SQL_INSTANCE" --project "$GCP_PROJECT_ID" --user=postgres --database="$DB_PROD" <<'SQL'
GRANT CONNECT ON DATABASE $DB_PROD TO $APP_USER_PROD;
GRANT CONNECT ON DATABASE $DB_PROD TO $MIGRATOR_USER_PROD;

GRANT USAGE ON SCHEMA public TO $APP_USER_PROD;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO $APP_USER_PROD;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO $APP_USER_PROD;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO $APP_USER_PROD;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO $APP_USER_PROD;

GRANT USAGE, CREATE ON SCHEMA public TO $MIGRATOR_USER_PROD;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON ALL TABLES IN SCHEMA public TO $MIGRATOR_USER_PROD;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO $MIGRATOR_USER_PROD;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLES TO $MIGRATOR_USER_PROD;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO $MIGRATOR_USER_PROD;
SQL
EOF

echo
echo "Cloud SQL connection name: $CONNECTION_NAME"
echo
echo "Cloud Run env var examples (password from Secret Manager):"
echo "  DATABASE_URL=postgresql+asyncpg://<app-user>:<password>@/$DB_STAGING?host=/cloudsql/$CONNECTION_NAME"
echo "  ALEMBIC_DATABASE_URL=postgresql+psycopg://<migrator-user>:<password>@/$DB_STAGING?host=/cloudsql/$CONNECTION_NAME"
