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
#   APPLY_SQL_GRANTS=true
#   BOOTSTRAP_POSTGRES_ADMIN_PASSWORD=false
#   POSTGRES_ADMIN_PASS_SECRET=DB_POSTGRES_ADMIN_PASS
#   POSTGRES_ADMIN_PASS=<optional plaintext override>
#   SQL_PROXY_PORT=9543

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
APPLY_SQL_GRANTS="${APPLY_SQL_GRANTS:-true}"
BOOTSTRAP_POSTGRES_ADMIN_PASSWORD="${BOOTSTRAP_POSTGRES_ADMIN_PASSWORD:-false}"
POSTGRES_ADMIN_PASS_SECRET="${POSTGRES_ADMIN_PASS_SECRET:-DB_POSTGRES_ADMIN_PASS}"
POSTGRES_ADMIN_PASS="${POSTGRES_ADMIN_PASS:-}"
SQL_PROXY_PORT="${SQL_PROXY_PORT:-9543}"
CONNECTION_NAME=""
SQL_PROXY_PID=""

if [[ -z "$CLOUD_SQL_INSTANCE" ]]; then
  echo "CLOUD_SQL_INSTANCE is required."
  echo "Example: CLOUD_SQL_INSTANCE=adv-assistant-pg scripts/provision_db_access.sh"
  exit 1
fi

cleanup() {
  if [[ -n "$SQL_PROXY_PID" ]] && kill -0 "$SQL_PROXY_PID" >/dev/null 2>&1; then
    kill "$SQL_PROXY_PID" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

validate_sql_identifier() {
  local value="$1"
  local label="$2"
  if [[ ! "$value" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
    echo "Invalid $label: '$value'. Use simple SQL identifier format."
    exit 1
  fi
}

require_cmd gcloud
require_cmd openssl

validate_sql_identifier "$DB_STAGING" "DB_STAGING"
validate_sql_identifier "$DB_PROD" "DB_PROD"
validate_sql_identifier "$APP_USER_STAGING" "APP_USER_STAGING"
validate_sql_identifier "$APP_USER_PROD" "APP_USER_PROD"
validate_sql_identifier "$MIGRATOR_USER_STAGING" "MIGRATOR_USER_STAGING"
validate_sql_identifier "$MIGRATOR_USER_PROD" "MIGRATOR_USER_PROD"

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

load_or_bootstrap_postgres_admin_password() {
  if [[ -n "$POSTGRES_ADMIN_PASS" ]]; then
    echo "Using postgres admin password from POSTGRES_ADMIN_PASS."
    return 0
  fi

  if secret_exists "$POSTGRES_ADMIN_PASS_SECRET"; then
    POSTGRES_ADMIN_PASS="$(gcloud secrets versions access latest \
      --secret "$POSTGRES_ADMIN_PASS_SECRET" \
      --project "$GCP_PROJECT_ID")"
    echo "Loaded postgres admin password from secret: $POSTGRES_ADMIN_PASS_SECRET"
    return 0
  fi

  if [[ "$BOOTSTRAP_POSTGRES_ADMIN_PASSWORD" == "true" ]]; then
    POSTGRES_ADMIN_PASS="$(openssl rand -hex 24)"
    gcloud sql users set-password postgres \
      --instance "$CLOUD_SQL_INSTANCE" \
      --password "$POSTGRES_ADMIN_PASS" \
      --project "$GCP_PROJECT_ID" >/dev/null
    upsert_secret_value "$POSTGRES_ADMIN_PASS_SECRET" "$POSTGRES_ADMIN_PASS"
    echo "Bootstrapped postgres admin password into secret: $POSTGRES_ADMIN_PASS_SECRET"
    return 0
  fi

  return 1
}

find_cloud_sql_proxy_bin() {
  if command -v cloud-sql-proxy >/dev/null 2>&1; then
    command -v cloud-sql-proxy
    return 0
  fi

  if [[ -x "$HOME/google-cloud-sdk/bin/cloud-sql-proxy" ]]; then
    echo "$HOME/google-cloud-sdk/bin/cloud-sql-proxy"
    return 0
  fi

  return 1
}

start_cloud_sql_proxy() {
  local proxy_bin
  proxy_bin="$(find_cloud_sql_proxy_bin)" || return 1

  "$proxy_bin" "$CONNECTION_NAME" --port "$SQL_PROXY_PORT" >/tmp/cloud-sql-proxy-provision.log 2>&1 &
  SQL_PROXY_PID=$!
  sleep 3
  if ! kill -0 "$SQL_PROXY_PID" >/dev/null 2>&1; then
    echo "Cloud SQL Proxy failed to start."
    cat /tmp/cloud-sql-proxy-provision.log
    return 1
  fi
}

apply_db_grants() {
  local db="$1"
  local app_user="$2"
  local migrator_user="$3"

  PGPASSWORD="$POSTGRES_ADMIN_PASS" psql \
    -h 127.0.0.1 \
    -p "$SQL_PROXY_PORT" \
    -U postgres \
    -d "$db" \
    -v ON_ERROR_STOP=1 <<SQL
REVOKE cloudsqlsuperuser FROM $app_user;
REVOKE cloudsqlsuperuser FROM $migrator_user;

ALTER ROLE $app_user NOCREATEROLE NOCREATEDB;
ALTER ROLE $migrator_user NOCREATEROLE NOCREATEDB;

GRANT CONNECT ON DATABASE $db TO $app_user;
GRANT CONNECT ON DATABASE $db TO $migrator_user;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO $app_user;
REVOKE CREATE ON SCHEMA public FROM $app_user;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO $app_user;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO $app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO $app_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO $app_user;

GRANT USAGE, CREATE ON SCHEMA public TO $migrator_user;
GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
  ON ALL TABLES IN SCHEMA public TO $migrator_user;
GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO $migrator_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER ON TABLES TO $migrator_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO $migrator_user;
SQL
}

print_manual_grants() {
  echo
  echo "Manual SQL grant/hardening commands:"
  echo "Staging grants:"
  cat <<EOF
gcloud sql connect "$CLOUD_SQL_INSTANCE" --project "$GCP_PROJECT_ID" --user=postgres --database="$DB_STAGING" <<'SQL'
REVOKE cloudsqlsuperuser FROM $APP_USER_STAGING;
REVOKE cloudsqlsuperuser FROM $MIGRATOR_USER_STAGING;
ALTER ROLE $APP_USER_STAGING NOCREATEROLE NOCREATEDB;
ALTER ROLE $MIGRATOR_USER_STAGING NOCREATEROLE NOCREATEDB;

GRANT CONNECT ON DATABASE $DB_STAGING TO $APP_USER_STAGING;
GRANT CONNECT ON DATABASE $DB_STAGING TO $MIGRATOR_USER_STAGING;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO $APP_USER_STAGING;
REVOKE CREATE ON SCHEMA public FROM $APP_USER_STAGING;
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
REVOKE cloudsqlsuperuser FROM $APP_USER_PROD;
REVOKE cloudsqlsuperuser FROM $MIGRATOR_USER_PROD;
ALTER ROLE $APP_USER_PROD NOCREATEROLE NOCREATEDB;
ALTER ROLE $MIGRATOR_USER_PROD NOCREATEROLE NOCREATEDB;

GRANT CONNECT ON DATABASE $DB_PROD TO $APP_USER_PROD;
GRANT CONNECT ON DATABASE $DB_PROD TO $MIGRATOR_USER_PROD;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;

GRANT USAGE ON SCHEMA public TO $APP_USER_PROD;
REVOKE CREATE ON SCHEMA public FROM $APP_USER_PROD;
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
if [[ "$APPLY_SQL_GRANTS" == "true" ]]; then
  if load_or_bootstrap_postgres_admin_password; then
    if ! command -v psql >/dev/null 2>&1; then
      echo "Skipping automatic SQL grants: psql not found in PATH."
      print_manual_grants
    elif ! start_cloud_sql_proxy; then
      echo "Skipping automatic SQL grants: cloud-sql-proxy could not start."
      print_manual_grants
    else
      echo "Applying SQL grants and least-privilege hardening..."
      apply_db_grants "$DB_STAGING" "$APP_USER_STAGING" "$MIGRATOR_USER_STAGING"
      apply_db_grants "$DB_PROD" "$APP_USER_PROD" "$MIGRATOR_USER_PROD"
      echo "Applied SQL grants and hardening for staging + production."
    fi
  else
    echo "Skipping automatic SQL grants: postgres admin password unavailable."
    echo "Set BOOTSTRAP_POSTGRES_ADMIN_PASSWORD=true (recommended first run),"
    echo "or set POSTGRES_ADMIN_PASS / POSTGRES_ADMIN_PASS_SECRET."
    print_manual_grants
  fi
else
  echo "APPLY_SQL_GRANTS=false, skipping SQL grants/hardening."
  print_manual_grants
fi

echo
echo "Provisioning complete."

echo
echo "Cloud SQL connection name: $CONNECTION_NAME"
echo
echo "Cloud Run env var examples (password from Secret Manager):"
echo "  DATABASE_URL=postgresql+asyncpg://<app-user>:<password>@/$DB_STAGING?host=/cloudsql/$CONNECTION_NAME"
echo "  ALEMBIC_DATABASE_URL=postgresql+psycopg://<migrator-user>:<password>@/$DB_STAGING?host=/cloudsql/$CONNECTION_NAME"
