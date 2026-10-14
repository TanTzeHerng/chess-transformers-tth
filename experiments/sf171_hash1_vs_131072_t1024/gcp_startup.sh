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
URL='https://raw.githubusercontent.com/TanTzeHerng/chess-transformers-tth/experiment/sf171-mpv256-hash1-vs-131072-20261014/experiments/sf171_hash1_vs_131072_t1024'
for file in runner_gcp.py gcs_sync.py gcp_match_job.sh gcp_watchdog.sh; do
  curl -fsSL --retry 5 --retry-delay 4 "$URL/$file" -o "$file"
done
test -s runner_gcp.py
test -s gcs_sync.py
test -s gcp_match_job.sh
test -s gcp_watchdog.sh
chmod 700 gcp_match_job.sh
echo "SF171_STARTUP_FILES_DOWNLOADED; scheduling independent systemd job"
systemd-run --unit=sf171-mpv256 --description='Stockfish 17.1 MultiPV 256 two paired chess games' \
  --property=Restart=no \
  /bin/bash "$ROOT/gcp_match_job.sh" "$BUCKET"
systemd-run --unit=sf171-mpv256-guard --description="Stockfish VM automatic shutdown on match failure" \
  --property=Restart=no \
  /bin/bash "$ROOT/gcp_watchdog.sh"
ok=1
echo "SF171_MPV256_STARTUP_SCHEDULED systemd units sf171-mpv256 and sf171-mpv256-guard"
