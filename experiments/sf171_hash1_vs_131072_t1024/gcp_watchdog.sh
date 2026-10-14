#!/usr/bin/env bash
# Independent safety process. A Stockfish OOM kill terminates the match service
# but must never leave an idle, billable high-memory VM running indefinitely.
set -Eeuo pipefail
exec >>/var/log/sf171-watchdog.log 2>&1
echo "WATCHDOG_STARTED $(date -u +%FT%TZ)"
sleep 25
while true; do
  state="$(systemctl show -p ActiveState --value sf171-mpv256-100.service 2>/dev/null || true)"
  result="$(systemctl show -p Result --value sf171-mpv256-100.service 2>/dev/null || true)"
  if [[ "$state" != active && "$state" != activating ]]; then
    echo "MATCH_NOT_RUNNING $(date -u +%FT%TZ) active_state=$state result=$result"
    # The OOM killer terminates the match and its uploader together.
    # Preserve kernel evidence and PGN/CSV via this independent service.
    mkdir -p /opt/sf171/results_mpv256_100
    journalctl -u sf171-mpv256-100.service -n 140 --no-pager > /opt/sf171/results_mpv256_100/service_failure.log 2>&1 || true
    journalctl -k -b -n 250 --no-pager > /opt/sf171/results_mpv256_100/kernel_diagnostics.log 2>&1 || true
    BUCKET="$(curl -fsS -H "Metadata-Flavor: Google" \
      http://metadata.google.internal/computeMetadata/v1/instance/attributes/sf171-results-bucket 2>/dev/null || true)"
    if [[ -n "$BUCKET" ]]; then
      timeout 120 python3 /opt/sf171/gcs_sync.py --bucket "$BUCKET" --folder /opt/sf171/results_mpv256_100 --once || true
    fi
    sync || true
    /sbin/shutdown -h now || systemctl poweroff || true
    exit 0
  fi
  sleep 15
done
