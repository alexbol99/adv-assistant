#!/usr/bin/env bash
set -euo pipefail

# Runs Alembic migrations against Cloud SQL Postgres using a dedicated migrator user.
#
# Required env vars:
#   GCP_PROJECT_ID
#   CLOUD_SQL_CONNECTION_NAME
#   DB_NAME
#   DB_MIGRATOR_USER
#   DB_MIGRATOR_PASS_SECRET
#
# Optional:
#   SQL_PROXY_PORT=5432
#   ALEMBIC_REVISION=head
#   MIGRATION_RETRIES=5
#   CLOUD_SQL_PROXY_VERSION=2.18.3

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
: "${CLOUD_SQL_CONNECTION_NAME:?CLOUD_SQL_CONNECTION_NAME is required}"
: "${DB_NAME:?DB_NAME is required}"
: "${DB_MIGRATOR_USER:?DB_MIGRATOR_USER is required}"
: "${DB_MIGRATOR_PASS_SECRET:?DB_MIGRATOR_PASS_SECRET is required}"

SQL_PROXY_PORT="${SQL_PROXY_PORT:-5432}"
ALEMBIC_REVISION="${ALEMBIC_REVISION:-head}"
MIGRATION_RETRIES="${MIGRATION_RETRIES:-5}"
CLOUD_SQL_PROXY_VERSION="${CLOUD_SQL_PROXY_VERSION:-2.18.3}"

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing required command: $1"
    exit 1
  fi
}

require_cmd gcloud
require_cmd uv
require_cmd curl

DB_PASSWORD="$(gcloud secrets versions access latest \
  --project "$GCP_PROJECT_ID" \
  --secret "$DB_MIGRATOR_PASS_SECRET")"

if [[ -z "$DB_PASSWORD" ]]; then
  echo "Resolved empty DB password from secret: $DB_MIGRATOR_PASS_SECRET"
  exit 1
fi

sha256_file() {
  local path="$1"
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$path" | awk '{print $1}'
    return 0
  fi
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$path" | awk '{print $1}'
    return 0
  fi
  echo "Missing checksum utility (sha256sum/shasum)."
  return 1
}

verify_proxy_checksum() {
  local artifact_name="$1"
  local binary_path="$2"
  local checksums_file
  checksums_file="$(mktemp /tmp/cloud-sql-proxy-checksums.XXXXXX)"

  curl -fsSL \
    "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v${CLOUD_SQL_PROXY_VERSION}/checksums.txt" \
    -o "$checksums_file"

  local expected_checksum
  expected_checksum="$(grep -E "[[:space:]]\\*?${artifact_name}\$" "$checksums_file" | awk '{print $1}' | head -n 1)"
  rm -f "$checksums_file"

  if [[ -z "$expected_checksum" ]]; then
    echo "Unable to resolve expected checksum for ${artifact_name}"
    return 1
  fi

  local actual_checksum
  actual_checksum="$(sha256_file "$binary_path")"
  if [[ "$actual_checksum" != "$expected_checksum" ]]; then
    echo "Checksum verification failed for Cloud SQL Proxy binary."
    echo "expected=$expected_checksum"
    echo "actual=$actual_checksum"
    return 1
  fi
}

resolve_proxy_download_artifact() {
  local os
  local arch
  os="$(uname -s | tr '[:upper:]' '[:lower:]')"
  arch="$(uname -m)"

  case "$arch" in
    x86_64)
      arch="amd64"
      ;;
    arm64|aarch64)
      arch="arm64"
      ;;
    *)
      echo "Unsupported architecture for automatic Cloud SQL Proxy download: $arch"
      return 1
      ;;
  esac

  case "$os" in
    linux|darwin)
      echo "cloud-sql-proxy.${os}.${arch}"
      ;;
    *)
      echo "Unsupported OS for automatic Cloud SQL Proxy download: $os"
      return 1
      ;;
  esac
}

PROXY_BIN="${CLOUD_SQL_PROXY_BIN:-}"
if [[ -z "$PROXY_BIN" ]]; then
  if command -v cloud-sql-proxy >/dev/null 2>&1; then
    PROXY_BIN="$(command -v cloud-sql-proxy)"
  else
    PROXY_BIN="./cloud-sql-proxy"
  fi
fi

if [[ ! -x "$PROXY_BIN" ]]; then
  artifact_name="$(resolve_proxy_download_artifact)"
  curl -fsSL \
    "https://storage.googleapis.com/cloud-sql-connectors/cloud-sql-proxy/v${CLOUD_SQL_PROXY_VERSION}/${artifact_name}" \
    -o "$PROXY_BIN"
  verify_proxy_checksum "$artifact_name" "$PROXY_BIN"
  chmod +x "$PROXY_BIN"
fi

PROXY_LOG_FILE="$(mktemp /tmp/cloud-sql-proxy.XXXXXX.log)"
"$PROXY_BIN" "$CLOUD_SQL_CONNECTION_NAME" --port "$SQL_PROXY_PORT" >"$PROXY_LOG_FILE" 2>&1 &
PROXY_PID="$!"
cleanup() {
  kill "$PROXY_PID" >/dev/null 2>&1 || true
  rm -f "$PROXY_LOG_FILE"
}
trap cleanup EXIT
sleep 3

# Verify that the Cloud SQL Proxy started successfully before proceeding.
if ! kill -0 "$PROXY_PID" >/dev/null 2>&1; then
  echo "Cloud SQL Proxy process (PID=${PROXY_PID}) is not running after startup delay."
  echo "Cloud SQL Proxy log:"
  cat "$PROXY_LOG_FILE" || true
  exit 1
fi

MIGRATION_URL="postgresql+psycopg://${DB_MIGRATOR_USER}:${DB_PASSWORD}@127.0.0.1:${SQL_PROXY_PORT}/${DB_NAME}"

attempt=1
while [[ "$attempt" -le "$MIGRATION_RETRIES" ]]; do
  if ALEMBIC_DATABASE_URL="$MIGRATION_URL" uv run alembic upgrade "$ALEMBIC_REVISION"; then
    echo "Migration completed: revision=$ALEMBIC_REVISION db=$DB_NAME"
    exit 0
  fi
  echo "Migration attempt ${attempt}/${MIGRATION_RETRIES} failed; retrying."
  attempt=$((attempt + 1))
  sleep 2
done

echo "Migration failed after ${MIGRATION_RETRIES} attempts."
cat "$PROXY_LOG_FILE" || true
exit 1
