#!/usr/bin/env bash
# Cloud Shell launcher for 100-game SF171 (Hash=1 versus Hash=131072 MB).
# This WILL create chargeable Google Cloud resources if you type START.
# Use ONLY while the billing account is an active unupgraded Free Trial.
# Free Trial credit covers eligible usage; the VM is force-stopped within 12 hours.
set -Eeuo pipefail
export CLOUDSDK_CORE_DISABLE_PROMPTS=1

PROJECT='project-6e20fc86-4615-4cfd-9ee'
ZONE='us-central1-a'
REGION='us-central1'
MACHINE='n2-custom-8-163840-ext'
VM_NAME='sf171-h1-vs-h128k'
SA_NAME='sf171-uploader'
BUCKET="$PROJECT-sf171-results"
SOURCE_URL='https://raw.githubusercontent.com/TanTzeHerng/chess-transformers-tth/experiment/gcp-sf171-hash1-vs-131072-20261014/experiments/sf171_hash1_vs_131072_t1024/gcp_startup.sh'

current_project="$(gcloud config get-value project 2>/dev/null)"
if [[ "$current_project" != "$PROJECT" ]]; then
  printf 'INCORRECT_PROJECT: expected %s, found %s; NO changes made\n' "$PROJECT" "$current_project" >&2
  exit 1
fi
enabled="$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)')"
if [[ "$enabled" != True && "$enabled" != true ]]; then
  echo 'PROJECT_BILLING_NOT_ENABLED: refusing to create any resources' >&2
  exit 1
fi
api="$(gcloud services list --enabled --project="$PROJECT" \
  --filter='config.name:compute.googleapis.com' \
  --format='value(config.name)')"
if [[ "$api" != compute.googleapis.com ]]; then
  echo 'COMPUTE_API_NOT_ENABLED' >&2
  exit 1
fi

quota_json="$(mktemp)"
gcloud compute project-info describe --project="$PROJECT" --format=json > "$quota_json"
all_cores="$(python3 -c 'import json,sys;q=json.load(open(sys.argv[1]))["quotas"];print(next(x["limit"]-x["usage"] for x in q if x["metric"]=="CPUS_ALL_REGIONS"))' "$quota_json")"
rm -f "$quota_json"
if ! awk -v n="$all_cores" 'BEGIN {exit !(n >= 8)}'; then
  echo "INSUFFICIENT_GLOBAL_VCPU_QUOTA: $all_cores available, need 8" >&2
  exit 1
fi
n2_free="$(gcloud compute regions describe "$REGION" --project="$PROJECT" \
    --format=json | python3 -c 'import json,sys;q=json.load(sys.stdin)["quotas"];print(next(x["limit"]-x["usage"] for x in q if x["metric"]=="N2_CPUS"))')"
if ! awk -v n="$n2_free" 'BEGIN {exit !(n >= 8)}'; then
  echo "INSUFFICIENT_N2_CPU_QUOTA: $n2_free available, need 8" >&2
  exit 1
fi
gcloud compute networks describe default --project="$PROJECT" --format='value(name)' >/dev/null

if gcloud compute instances describe "$VM_NAME" --zone="$ZONE" --project="$PROJECT" >/dev/null 2>&1; then
  echo "INSTANCE_ALREADY_EXISTS: $VM_NAME. Not creating a duplicate." >&2
  exit 1
fi

printf '\nPROJECT=%s\nZONE=%s\nMACHINE=%s\n' "$PROJECT" "$ZONE" "$MACHINE"
printf 'EXPECTED_PHYSICAL_RAM=160 GiB, STOCKFISH_HASH=128 GiB, VCPUS=8, UCI_THREADS=1024\n'
printf 'MATCH=100 chess-rule-verified games, depth1, MultiPV1, 5-piece Syzygy, 50 paired openings\n'
printf 'VM_MAX_RUN=12 hours with STOP action; it also shuts down when the match finishes.\n'
printf 'GCS_RESULTS_BUCKET=gs://%s\n' "$BUCKET"
printf 'IMPORTANT: Cloud storage and the 30 GiB boot disk may continue using trial credits until deleted.\n'
printf 'IMPORTANT: If VM creation is rejected, STOP; do not upgrade billing to bypass restrictions.\n'
printf 'IMPORTANT: This uses promotional Free Trial credits, not an always-free VM.\n\n'

printf 'You have confirmed in the Google Cloud console that the billing account is still Free Trial, NOT Paid, with remaining credits.\n'
printf 'Only if you want the 100-game match to begin using promotional credit, type START: '
read -r ANSWER
if [[ "$ANSWER" != START ]]; then
  echo 'CANCELLED: no resources created.'
  exit 0
fi

# Prep only after explicit START. A failure here stops before the expensive VM.
if ! gcloud storage buckets describe "gs://$BUCKET" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://$BUCKET" --project="$PROJECT" \
    --location="$REGION" --uniform-bucket-level-access \
    --default-storage-class=STANDARD
fi
SA_EMAIL="$SA_NAME@$PROJECT.iam.gserviceaccount.com"
if ! gcloud iam service-accounts describe "$SA_EMAIL" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SA_NAME" \
    --project="$PROJECT" --display-name='Stockfish match GCS result uploader'
fi
gcloud storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$SA_EMAIL" \
  --role='roles/storage.objectAdmin' \
  --project="$PROJECT"

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
curl -fsSL --retry 3 "$SOURCE_URL" -o "$TMPDIR/gcp_startup.sh"
test -s "$TMPDIR/gcp_startup.sh"
bash -n "$TMPDIR/gcp_startup.sh"

# Try alternate Google Cloud zones when a valid large VM has no free physical host.
# At most ONE VM may be created. Each failed capacity request creates zero VMs.
ZONES=(us-central1-b us-central1-c us-central1-f us-east1-b us-east1-c us-east1-d asia-southeast1-a asia-southeast1-b asia-southeast1-c)
created_zone=''
declare -A checked_region
for candidate in "${ZONES[@]}"; do
  region="${candidate%-*}"
  if [[ -z "${checked_region[$region]+set}" ]]; then
    checked_region[$region]=1
    n2_available="$(gcloud compute regions describe "$region" --project="$PROJECT" --format=json | python3 -c 'import json,sys;q=json.load(sys.stdin)["quotas"];print(next(x["limit"]-x["usage"] for x in q if x["metric"]=="N2_CPUS"))')"
    if ! awk -v n="$n2_available" 'BEGIN {exit !(n >= 8)}'; then
      echo "SKIP_REGION_INSUFFICIENT_N2_QUOTA $region free_cores=$n2_available"
      continue
    fi
  fi
  zone_status="$(gcloud compute zones describe "$candidate" --project="$PROJECT" --format='value(status)' 2>/dev/null || true)"
  if [[ "$zone_status" != UP ]]; then
    echo "SKIP_ZONE_NOT_UP $candidate status=$zone_status"
    continue
  fi
  existing="$(gcloud compute instances list --project="$PROJECT" --filter="name=$VM_NAME" --format='value(zone.basename())')"
  if [[ -n "$existing" ]]; then
    echo "STOP_INSTANCE_ALREADY_EXISTS name=$VM_NAME zone=$existing"
    exit 2
  fi
  logfile="$TMPDIR/create-${candidate}.log"
  echo "TRYING_ZONE $candidate MACHINE=$MACHINE"
  if gcloud compute instances create "$VM_NAME" \
      --project="$PROJECT" \
      --zone="$candidate" \
      --machine-type="$MACHINE" \
      --image-family=debian-12 \
      --image-project=debian-cloud \
      --boot-disk-size=30GB \
      --boot-disk-type=pd-balanced \
      --service-account="$SA_EMAIL" \
      --scopes='https://www.googleapis.com/auth/cloud-platform' \
      --metadata=sf171-results-bucket="$BUCKET" \
      --metadata-from-file=startup-script="$TMPDIR/gcp_startup.sh" \
      --max-run-duration=12h \
      --instance-termination-action=STOP \
      --no-restart-on-failure \
      --labels=purpose=sf171-selfplay \
      2>&1 | tee "$logfile"; then
    created_zone="$candidate"
    echo "INSTANCE_CREATED_SUCCESSFULLY name=$VM_NAME zone=$created_zone"
    break
  fi
  existing="$(gcloud compute instances list --project="$PROJECT" --filter="name=$VM_NAME" --format='value(zone.basename())')"
  if [[ -n "$existing" ]]; then
    echo "INSTANCE_VISIBLE_AFTER_ERROR zone=$existing; not risking duplicate"
    exit 2
  fi
  if grep -qE "ZONE_RESOURCE_POOL_EXHAUSTED|resource_availability" "$logfile"; then
    echo "CAPACITY_UNAVAILABLE $candidate; trying next zone"
  else
    echo "NON_CAPACITY_ERROR $candidate; aborting. Exact error above."
    exit 3
  fi
done
if [[ -z "$created_zone" ]]; then
  echo "ALL_ZONES_EXHAUSTED: No VM created; no VM CPU/RAM charges."
  echo "Storage bucket and service account remain ready for a future attempt."
  exit 4
fi
ZONE="$created_zone"
echo "VM_CREATION_COMMAND_SUCCEEDED"
echo "Status: gcloud compute instances describe $VM_NAME --zone=$ZONE --format='value(status)'"
echo "Live VM startup: gcloud compute instances get-serial-port-output $VM_NAME --zone=$ZONE"
echo "Saved outputs: gcloud storage ls gs://$BUCKET/"
echo "After results downloaded, delete the VM and bucket to stop remaining storage charges."
