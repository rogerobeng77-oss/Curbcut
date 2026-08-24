#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?usage: teardown.sh <service-name>}"
PROJECT="${GCP_PROJECT:?set GCP_PROJECT to your Google Cloud project id}"
REGION="us-central1"

gcloud run services delete "$SERVICE" \
  --project="$PROJECT" --region="$REGION" --quiet

echo "Deleted ${SERVICE}. Verify no other services are running:"
gcloud run services list --project="$PROJECT"
