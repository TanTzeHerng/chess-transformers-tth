#!/usr/bin/env bash
# Read-only capacity probe for the Stockfish 17.1 Hash 1 vs 131072 MB experiment.
# Run in Google Cloud Shell. Never provisions anything, never upgrades billing.
set -euo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

printf '\n=== GOOGLE CLOUD TRIAL CAPACITY (READ-ONLY) ===\n'
PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
if [[ -z "$PROJECT_ID" || "$PROJECT_ID" == "(unset)" ]]; then
  printf 'No active project selected. Use the project selector at the top of Google Cloud Console first.\n'
  exit 1
fi
printf 'Project selected: %s\n' "$PROJECT_ID"

printf '\n=== BILLING ATTACHMENT (NOT TRIAL CONFIRMATION) ===\n'
if ! gcloud billing projects describe "$PROJECT_ID" --format=json 2>/dev/null | \
python3 -c 'import json,sys; d=json.load(sys.stdin);print("Billing enabled:",d.get("billingEnabled","unknown"));print("Billing account linked:",bool(d.get("billingAccountName")))' ; then
  printf 'Billing details not readable. Check the Billing tab manually.\n'
fi
printf 'WARNING: Billing enabled does NOT prove your account is Free Trial. Confirm the Free Trial banner and remaining credits in Billing.\n'

printf '\n=== COMPUTE ENGINE API ===\n'
API_ENABLED="$(gcloud services list --enabled --project="$PROJECT_ID" --filter='config.name:compute.googleapis.com' --format='value(config.name)' 2>/dev/null || true)"
if [[ -z "$API_ENABLED" ]]; then
  printf 'Compute Engine API is not enabled (or current account cannot inspect it).\n'
  printf 'Open https://console.cloud.google.com/apis/library/compute.googleapis.com and check its status.\n'
  printf 'No changes were made.\n'
  exit 0
fi
printf 'Compute Engine API is enabled.\n'

printf '\n=== PROJECT-WIDE CPU QUOTAS ===\n'
gcloud compute project-info describe --project="$PROJECT_ID" --format=json 2>/dev/null | \
python3 -c '
import sys,json
d=json.load(sys.stdin)
for q in d.get("quotas",[]):
    name=q.get("metric","")
    if "CPU" in name or "INSTANCE" in name:
        print(f"{name:38} limit={q.get('limit')} usage={q.get('usage')}")
' || printf 'Project-wide quota unavailable.\n'

for REGION in us-central1 us-east1 asia-southeast1; do
  printf '\n=== %s CPU QUOTAS ===\n' "$REGION"
  gcloud compute regions describe "$REGION" --project="$PROJECT_ID" --format=json 2>/dev/null | \
  python3 -c '
import sys,json
d=json.load(sys.stdin)
for q in d.get("quotas",[]):
    name=q.get("metric","")
    if "CPU" in name or "INSTANCE" in name:
        print(f"{name:38} limit={q.get('limit')} usage={q.get('usage')}")
' || printf 'Regional quotas unavailable in %s.\n' "$REGION"
done
printf '\nREAD_ONLY_PROBE_COMPLETE: No resources created, no billing changes, no charge.\n'
