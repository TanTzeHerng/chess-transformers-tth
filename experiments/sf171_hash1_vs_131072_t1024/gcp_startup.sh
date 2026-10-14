#!/usr/bin/env bash
# Compute Engine metadata startup script. Installs no background work on local PC.
set -Eeuo pipefail
exec > >(tee -a /var/log/sf171-startup.log) 2>&1
ok=0
shutdown_if_bootstrap_failed() {
  if [[ "$ok" != 1 ]]; then
    echo "SF171_BOOTSTRAP_FAILED; stopping this VM to prevent wasting promotional credits."
    /sbin/shutdown -h now || true
  fi
}
trap shutdown_if_bootstrap_failed EXIT

METADATA='http://metadata.google.internal/computeMetadata/v1/instance/attributes/sf171-results-bucket'
BUCKET="$(curl -fsS -H 'Metadata-Flavor: Google' "$METADATA")"
test -n "$BUCKET"
ROOT=/opt/sf171
mkdir -p "$ROOT/results"
cd "$ROOT"
URL='https://raw.githubusercontent.com/TanTzeHerng/chess-transformers-tth/experiment/gcp-sf171-hash1-vs-131072-20261014/experiments/sf171_hash1_vs_131072_t1024'
for file in runner_gcp.py gcs_sync.py gcp_match_job.sh; do
  curl -fsSL --retry 5 --retry-delay 4 "$URL/$file" -o "$file"
done
test -s runner_gcp.py
test -s gcs_sync.py
test -s gcp_match_job.sh
chmod 700 gcp_match_job.sh
echo "SF171_STARTUP_FILES_DOWNLOADED; scheduling independent systemd job"
systemd-run --unit=sf171-match --description='Stockfish 17.1 Hash 1 vs 131072 100 verified chess games' \
  --property=Restart=no \
  /bin/bash "$ROOT/gcp_match_job.sh" "$BUCKET"
ok=1
echo "SF171_STARTUP_SCHEDULED systemd unit sf171-match"
