#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Verify Secret Manager existence and Cloud Run secret bindings for staging/prod.

Usage:
  scripts/verify_cloudrun_secret_bindings.sh \
    --target <staging|prod> \
    --project-id <gcp-project-id> \
    --region <cloud-run-region> \
    --worker-service <cloud-run-worker-service> \
    --webhook-service <cloud-run-webhook-service> \
    [--require-prod-whatsapp]

Inputs:
  The script reads secret-name mappings from environment variables, using the
  same naming convention as CI:

  For --target staging:
    STAGING_SECRET_DATABASE_URL                    (required)
    STAGING_SECRET_WHATSAPP_ACCESS_TOKEN          (required)
    STAGING_SECRET_VERIFY_TOKEN                   (optional)
    STAGING_SECRET_META_APP_SECRET                (optional)
    STAGING_SECRET_ADMIN_BASIC_USERNAME           (optional)
    STAGING_SECRET_ADMIN_BASIC_PASSWORD           (optional)
    STAGING_SECRET_OPENAI_API_KEY                 (optional)
    STAGING_SECRET_GEMINI_API_KEY                 (optional)
    STAGING_SECRET_NANA_BANANA_API_KEY            (optional)
    STAGING_SECRET_CMS_CITYSCREEN_APP_TOKEN       (optional)

  For --target prod:
    PROD_SECRET_DATABASE_URL                      (required)
    PROD_SECRET_WHATSAPP_ACCESS_TOKEN             (optional; required with --require-prod-whatsapp)
    PROD_SECRET_VERIFY_TOKEN                      (optional)
    PROD_SECRET_META_APP_SECRET                   (optional)
    PROD_SECRET_ADMIN_BASIC_USERNAME              (optional)
    PROD_SECRET_ADMIN_BASIC_PASSWORD              (optional)
    PROD_SECRET_OPENAI_API_KEY                    (optional)
    PROD_SECRET_GEMINI_API_KEY                    (optional)
    PROD_SECRET_NANA_BANANA_API_KEY               (optional)
    PROD_SECRET_CMS_CITYSCREEN_APP_TOKEN          (optional)

Example:
  STAGING_SECRET_DATABASE_URL=DATABASE_URL_STAGING \
  STAGING_SECRET_WHATSAPP_ACCESS_TOKEN=WHATSAPP_ACCESS_TOKEN_STAGING \
  STAGING_SECRET_META_APP_SECRET=META_APP_SECRET_STAGING \
  scripts/verify_cloudrun_secret_bindings.sh \
    --target staging \
    --project-id adv-assistant-staging-488908 \
    --region me-west1 \
    --worker-service adv-assistant-worker-staging \
    --webhook-service adv-assistant-webhook-staging
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing required command: $cmd" >&2
    exit 1
  fi
}

TARGET=""
PROJECT_ID=""
REGION=""
WORKER_SERVICE=""
WEBHOOK_SERVICE=""
REQUIRE_PROD_WHATSAPP="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)
      TARGET="${2:-}"
      shift 2
      ;;
    --project-id)
      PROJECT_ID="${2:-}"
      shift 2
      ;;
    --region)
      REGION="${2:-}"
      shift 2
      ;;
    --worker-service)
      WORKER_SERVICE="${2:-}"
      shift 2
      ;;
    --webhook-service)
      WEBHOOK_SERVICE="${2:-}"
      shift 2
      ;;
    --require-prod-whatsapp)
      REQUIRE_PROD_WHATSAPP="true"
      shift 1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

if [[ "$TARGET" != "staging" && "$TARGET" != "prod" ]]; then
  echo "--target must be staging or prod" >&2
  exit 1
fi

if [[ -z "$PROJECT_ID" || -z "$REGION" || -z "$WORKER_SERVICE" || -z "$WEBHOOK_SERVICE" ]]; then
  echo "Missing required args. See --help." >&2
  exit 1
fi

require_cmd gcloud
require_cmd jq

PREFIX="$(printf '%s' "$TARGET" | tr '[:lower:]' '[:upper:]')"

WORKER_JSON="$(mktemp)"
WEBHOOK_JSON="$(mktemp)"
cleanup() {
  rm -f "$WORKER_JSON" "$WEBHOOK_JSON"
}
trap cleanup EXIT

echo "Fetching Cloud Run service specs..."
gcloud run services describe "$WORKER_SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format=json > "$WORKER_JSON"
gcloud run services describe "$WEBHOOK_SERVICE" \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --format=json > "$WEBHOOK_JSON"

get_env_var_value() {
  local var_name="$1"
  printf '%s' "${!var_name:-}"
}

is_required_for_target() {
  local repo_var="$1"
  if [[ "$repo_var" == "${PREFIX}_SECRET_DATABASE_URL" ]]; then
    return 0
  fi
  if [[ "$TARGET" == "staging" && "$repo_var" == "STAGING_SECRET_WHATSAPP_ACCESS_TOKEN" ]]; then
    return 0
  fi
  if [[ "$TARGET" == "prod" && "$REQUIRE_PROD_WHATSAPP" == "true" && "$repo_var" == "PROD_SECRET_WHATSAPP_ACCESS_TOKEN" ]]; then
    return 0
  fi
  return 1
}

service_has_binding() {
  local service_json="$1"
  local env_name="$2"
  local secret_name="$3"
  jq -e \
    --arg env_name "$env_name" \
    --arg secret_name "$secret_name" \
    '
      [
        .spec.template.spec.containers[]?.env[]?,
        .spec.template.containers[]?.env[]?,
        .template.containers[]?.env[]?
      ]
      | any(
          .name == $env_name and
          (
            (.valueFrom.secretKeyRef.name? // "") == $secret_name or
            (.valueFrom.secretKeyRef.secret? // "") == $secret_name or
            (.valueSource.secretKeyRef.name? // "") == $secret_name or
            (.valueSource.secretKeyRef.secret? // "") == $secret_name
          )
        )
    ' "$service_json" >/dev/null
}

check_secret_exists() {
  local secret_name="$1"
  gcloud secrets describe "$secret_name" \
    --project "$PROJECT_ID" \
    --format="value(name)" >/dev/null
}

validate_mapping() {
  local repo_var="$1"
  local runtime_env="$2"
  local scope="$3"
  local secret_name
  secret_name="$(get_env_var_value "$repo_var")"

  if [[ -z "$secret_name" ]]; then
    if is_required_for_target "$repo_var"; then
      echo "[FAIL] Required mapping is empty: $repo_var" >&2
      FAILURES=$((FAILURES + 1))
    else
      echo "[SKIP] Optional mapping not set: $repo_var"
    fi
    return
  fi

  if check_secret_exists "$secret_name"; then
    echo "[PASS] Secret exists: $secret_name (from $repo_var)"
  else
    echo "[FAIL] Secret missing in Secret Manager: $secret_name (from $repo_var)" >&2
    FAILURES=$((FAILURES + 1))
    return
  fi

  if [[ "$scope" == "worker" || "$scope" == "both" ]]; then
    if service_has_binding "$WORKER_JSON" "$runtime_env" "$secret_name"; then
      echo "[PASS] Worker binding: $runtime_env <- $secret_name"
    else
      echo "[FAIL] Worker missing/incorrect binding: $runtime_env <- $secret_name" >&2
      FAILURES=$((FAILURES + 1))
    fi
  fi

  if [[ "$scope" == "webhook" || "$scope" == "both" ]]; then
    if service_has_binding "$WEBHOOK_JSON" "$runtime_env" "$secret_name"; then
      echo "[PASS] Webhook binding: $runtime_env <- $secret_name"
    else
      echo "[FAIL] Webhook missing/incorrect binding: $runtime_env <- $secret_name" >&2
      FAILURES=$((FAILURES + 1))
    fi
  fi
}

FAILURES=0

MAPPINGS=(
  "${PREFIX}_SECRET_DATABASE_URL|DATABASE_URL|both"
  "${PREFIX}_SECRET_WHATSAPP_ACCESS_TOKEN|WHATSAPP_ACCESS_TOKEN|both"
  "${PREFIX}_SECRET_VERIFY_TOKEN|VERIFY_TOKEN|webhook"
  "${PREFIX}_SECRET_META_APP_SECRET|META_APP_SECRET|webhook"
  "${PREFIX}_SECRET_ADMIN_BASIC_USERNAME|ADMIN_BASIC_USERNAME|both"
  "${PREFIX}_SECRET_ADMIN_BASIC_PASSWORD|ADMIN_BASIC_PASSWORD|both"
  "${PREFIX}_SECRET_OPENAI_API_KEY|OPENAI_API_KEY|worker"
  "${PREFIX}_SECRET_GEMINI_API_KEY|GEMINI_API_KEY|worker"
  "${PREFIX}_SECRET_NANA_BANANA_API_KEY|NANA_BANANA_API_KEY|worker"
  "${PREFIX}_SECRET_CMS_CITYSCREEN_APP_TOKEN|CMS_CITYSCREEN_APP_TOKEN|worker"
)

echo "Verifying secrets and Cloud Run bindings for target=$TARGET..."
for mapping in "${MAPPINGS[@]}"; do
  IFS='|' read -r repo_var runtime_env scope <<< "$mapping"
  validate_mapping "$repo_var" "$runtime_env" "$scope"
done

if [[ "$FAILURES" -gt 0 ]]; then
  echo "Verification failed with $FAILURES issue(s)." >&2
  exit 1
fi

echo "Verification passed."
