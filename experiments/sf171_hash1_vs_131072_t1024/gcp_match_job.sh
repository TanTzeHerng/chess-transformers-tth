#!/usr/bin/env bash
# Runs inside ONE Google Compute Engine VM, never on user's Windows computer.
# Downloads verified official Stockfish 17.1 and the full Syzygy 3/4/5 set.
# Automatically checkpoints results to GCS and halts the VM on completion/failure.
set -Eeuo pipefail
export DEBIAN_FRONTEND=noninteractive
export CLOUDSDK_CORE_DISABLE_PROMPTS=1
ROOT=/opt/sf171
cd "$ROOT"
mkdir -p results
exec > >(tee -a results/cloud_job.log) 2>&1

BUCKET="$1"
if [[ -z "$BUCKET" ]]; then echo 'No GCS results bucket' >&2; exit 1; fi
printf 'CLOUD_JOB_START %s bucket=%s\n' "$(date -u +%FT%TZ)" "$BUCKET"
SYNC_PID=""

finish() {
  local exit_code=$?
  trap - EXIT
  printf 'CLOUD_JOB_FINISH %s code=%s\n' "$(date -u +%FT%TZ)" "$exit_code"
  if [[ "$exit_code" -eq 0 ]]; then
    printf 'MATCH_FINISHED_SUCCESSFULLY\n' | tee results/finish_state.txt
  else
    printf 'MATCH_INTERRUPTED_OR_FAILED exit_code=%s\n' "$exit_code" | tee results/finish_state.txt
  fi
  if [[ -n "$SYNC_PID" ]]; then
    kill "$SYNC_PID" 2>/dev/null || true
    wait "$SYNC_PID" 2>/dev/null || true
  fi
  # Attempt last upload, even when match fails, then halt VM to stop CPU/RAM billing.
  timeout 180 python3 gcs_sync.py --bucket "$BUCKET" --folder results --once || true
  sync || true
  printf 'SHUTTING_DOWN_GOOGLE_VM\n'
  /sbin/shutdown -h now || systemctl poweroff || true
}
trap finish EXIT

# Safety preflight: 160 GiB system OOM-killed the 128 GiB hash with 1024 threads.
# Require at least 200 GiB reported physical RAM. This is conservative, not a
# guarantee against another OOM; do not modify any chess-engine settings.
MEMTOTAL_KIB="$(awk '/^MemTotal:/{print $2}' /proc/meminfo)"
MINIMUM_KIB=$((200*1024*1024))
if (( MEMTOTAL_KIB < MINIMUM_KIB )); then
  printf "PREFLIGHT_REFUSED_RAM_TOO_SMALL reported_kib=%s needed_kib=%s\n" "$MEMTOTAL_KIB" "$MINIMUM_KIB" >&2
  exit 33
fi
printf "PREFLIGHT_SUFFICIENT_RAM reported_kib=%s\n" "$MEMTOTAL_KIB"

# Upload checkpoint files early, so partial PGN/CSV survives a premature stop.
python3 gcs_sync.py --bucket "$BUCKET" --folder results --interval 60 \
  > results/upload.log 2>&1 &
SYNC_PID="$!"

apt-get update -qq
apt-get install -y -qq curl wget ca-certificates python3 python3-pip python3-venv
python3 -m venv /opt/sf171-venv
/opt/sf171-venv/bin/python -m pip install --disable-pip-version-check 'python-chess==1.999'
export PATH="/opt/sf171-venv/bin:$PATH"
python -m py_compile runner_gcp.py gcs_sync.py

mkdir -p stockfish unpack tablebases
curl --retry 5 --retry-delay 4 --connect-timeout 30 -fLSs \
  'https://github.com/official-stockfish/Stockfish/releases/download/sf_17.1/stockfish-ubuntu-x86-64-avx2.tar' \
  -o sf171.tar
echo '09e953222dbe80aaea5c33dab265413b295cb8376418445c877708c61e31987d  sf171.tar' | sha256sum -c -
tar -xf sf171.tar -C unpack
BIN="$(find unpack -type f -name stockfish-ubuntu-x86-64-avx2 | head -1)"
test -n "$BIN"
install -m 755 "$BIN" stockfish/stockfish-ubuntu-x86-64-avx2
find unpack -type f -name '*.nnue' -exec cp -v {} stockfish/ \;
printf 'uci\nquit\n' | stockfish/stockfish-ubuntu-x86-64-avx2 \
  | grep -E 'Stockfish 17.1|option name Hash |option name Threads|option name MultiPV'
rm -rf unpack sf171.tar

for kind in wdl dtz; do
  url="https://tablebase.lichess.ovh/tables/standard/3-4-5-$kind/"
  timeout 1800 wget -q -nv -c -r -l1 -np -nd -e robots=off \
    --connect-timeout=15 --read-timeout=60 --tries=3 \
    -A '*.rtbw,*.rtbz' -P tablebases "$url"
done
test "$(find tablebases -maxdepth 1 -type f -name '*.rtbw' | wc -l)" -eq 145
test "$(find tablebases -maxdepth 1 -type f -name '*.rtbz' | wc -l)" -eq 145

printf 'RUNTIME_MEMORY\n'
free -h
printf 'RUNTIME_VCPUS='
nproc
printf 'RUNTIME_PID_MAX='
cat /proc/sys/kernel/threads-max
du -sh tablebases
printf 'ACTUAL_TOURNAMENT_START %s\n' "$(date -u +%FT%TZ)"
python -u runner_gcp.py
test -s results/all_100_games.pgn
grep -q '"completed": 100' results/status.json
printf 'ALL_100_VERIFIED_GAMES_COMPLETE\n'
