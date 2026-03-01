#!/usr/bin/env bash
set -euo pipefail

# Bootstrap helper for Phase 0 infrastructure.
# Required env vars:
#   GCP_PROJECT_ID
#   GCP_REGION (e.g. me-west1)
#   TASKS_REGION (e.g. me-central1, optional; defaults to GCP_REGION)
#   GCS_BUCKET
#   CLOUD_SQL_INSTANCE
#   TASKS_QUEUE

: "${GCP_PROJECT_ID:?GCP_PROJECT_ID is required}"
: "${GCP_REGION:?GCP_REGION is required}"
: "${TASKS_REGION:=$GCP_REGION}"
: "${GCS_BUCKET:?GCS_BUCKET is required}"
: "${CLOUD_SQL_INSTANCE:?CLOUD_SQL_INSTANCE is required}"
: "${TASKS_QUEUE:?TASKS_QUEUE is required}"

gcloud services enable \
  run.googleapis.com \
  cloudtasks.googleapis.com \
  sqladmin.googleapis.com \
  storage.googleapis.com \
  secretmanager.googleapis.com \
  --project "$GCP_PROJECT_ID"

gcloud sql instances create "$CLOUD_SQL_INSTANCE" \
  --database-version=POSTGRES_16 \
  --tier=db-f1-micro \
  --region="$GCP_REGION" \
  --project "$GCP_PROJECT_ID"

gsutil mb -p "$GCP_PROJECT_ID" -l "$GCP_REGION" "gs://$GCS_BUCKET"
gsutil iam ch allUsers:objectViewer "gs://$GCS_BUCKET"

gcloud tasks queues create "$TASKS_QUEUE" \
  --location="$TASKS_REGION" \
  --max-attempts=5 \
  --min-backoff=5s \
  --max-backoff=300s \
  --project "$GCP_PROJECT_ID"

echo "Bootstrap complete."
