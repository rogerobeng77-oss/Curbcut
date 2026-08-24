#!/usr/bin/env bash
set -euo pipefail

SERVICE="${1:?usage: deploy.sh <service-name>}"
PROJECT="${GCP_PROJECT:?set GCP_PROJECT to your Google Cloud project id}"
REGION="us-central1"

gcloud run deploy "$SERVICE" \
  --source . \
  --project="$PROJECT" \
  --region="$REGION" \
  --min-instances=0 \
  --max-instances=3 \
  --memory=2Gi \
  --timeout=600 \
  --set-env-vars="GOOGLE_GENAI_USE_VERTEXAI=true,GCP_PROJECT=${PROJECT},GCP_LOCATION=${REGION}" \
  --allow-unauthenticated

gcloud run services describe "$SERVICE" \
  --project="$PROJECT" --region="$REGION" --format='value(status.url)'
