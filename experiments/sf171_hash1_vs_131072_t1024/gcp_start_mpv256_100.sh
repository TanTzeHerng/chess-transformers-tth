#!/usr/bin/env bash
# Reuse STOPPED 224GiB Compute Engine VM for a new 100-game start-position MultiPV256 match.
# Deliberately does NOT create any new VM. Starts using Free Trial credit only after consent.
set -Eeuo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
PROJECT='project-6e20fc86-4615-4cfd-9ee'
ZONE='us-east1-b'
VM='sf171-h1-vs-h128k'
BUCKET="$PROJECT-sf171-results"
BRANCH='experiment/sf171-mpv256-100-startpos-20261014'
ROOT_URL="https://raw.githubusercontent.com/TanTzeHerng/chess-transformers-tth/$BRANCH/experiments/sf171_hash1_vs_131072_t1024"
if [[ "$(gcloud config get-value project 2>/dev/null)" != "$PROJECT" ]]; then
  echo 'WRONG_PROJECT: no changes made' >&2
  exit 1
fi
state="$(gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT" --format='value(status)')"
if [[ "$state" != TERMINATED ]]; then
  echo "REFUSING_TO_START: VM status=$state; must be TERMINATED. No changes made." >&2
  echo 'If the old tournament is still running, stop it before running this script.' >&2
  exit 2
fi
machine="$(gcloud compute instances describe "$VM" --zone="$ZONE" --project="$PROJECT" --format='value(machineType)')"
if [[ "$machine" != *n2-custom-8-229376-ext ]]; then
  echo "WRONG_MACHINE_TYPE: $machine (expected 224GiB N2 custom); no changes made" >&2
  exit 3
fi
gcloud storage buckets describe "gs://$BUCKET" --project="$PROJECT" >/dev/null
billing="$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)')"
if [[ "$billing" != True && "$billing" != true ]]; then
  echo 'BILLING_DISABLED; no changes made' >&2
  exit 4
fi
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT
curl -fsSL --retry 3 "$ROOT_URL/gcp_startup.sh" -o "$WORKDIR/mpv256_startup.sh"
bash -n "$WORKDIR/mpv256_startup.sh"
grep -q 'sf171-mpv256-100-startpos-20261014' "$WORKDIR/mpv256_startup.sh"
grep -q 'sf171-mpv256-100-guard' "$WORKDIR/mpv256_startup.sh"
printf '\nMultiPV256 100-game cloud tournament, all games from start position, prepared.\n'
printf 'VM=%s ZONE=%s TYPE=%s STATUS=%s\n' "$VM" "$ZONE" "$machine" "$state"
printf 'Chess: 100 games from initial position (no book); Stockfish17.1 depth1 Threads1024 MultiPV256 Hash1 vs Hash131072.\n'
printf 'Safety: 11h chess search cap + OOM shutdown watchdog + existing 12h VM runtime limit.\n'
printf 'Results: gs://%s/multipv256_100/ (does not overwrite old 11-game or 2-game files).\n' "$BUCKET"
printf 'THIS STARTS BILLABLE COMPUTE COVERED BY ELIGIBLE FREE TRIAL CREDIT.\n'
printf 'Confirm the Google account has NOT been upgraded to paid billing.\n'
printf 'Type START_100 to proceed: '
read -r REPLY
if [[ "$REPLY" != START_100 ]]; then
  echo 'CANCELLED: no VM started or VM metadata changed'
  exit 0
fi
echo 'Updating startup metadata for the isolated test (no VM restart yet)...'
gcloud compute instances add-metadata "$VM" --project="$PROJECT" --zone="$ZONE" \
  --metadata-from-file="startup-script=$WORKDIR/mpv256_startup.sh"
echo 'Starting the existing VM. No additional VM is created.'
gcloud compute instances start "$VM" --project="$PROJECT" --zone="$ZONE"
echo 'MPV256_100_VM_START_COMMAND_SUCCEEDED'
echo "STATUS: gcloud compute instances describe $VM --project=$PROJECT --zone=$ZONE --format='value(status)'"
echo "OUTPUTS: gcloud storage ls gs://$BUCKET/multipv256_100/"
echo "LOGS: gcloud storage cat gs://$BUCKET/multipv256_100/cloud_job.log | tail -n 35"
