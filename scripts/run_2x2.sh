#!/bin/bash
# 2x2: placement signal (pin counts vs projected bytes) x migration (off vs on).
# One cell per invocation; each cell may run several reps.
#
#   bash scripts/run_2x2.sh pin off     # A0 B0
#   bash scripts/run_2x2.sh pin on      # A0 B1
#   bash scripts/run_2x2.sh bytes off   # A1 B0
#   bash scripts/run_2x2.sh bytes on    # A1 B1
#
# Environment (all optional):
#   SSH_HOST / SSH_PORT / SSH_USER   default <ascend-host>:<ssh-port>:root
#   EXEC_ROOT                        default <server-dir>
#   REPS                             default 3
#   PROGRAMS / TURNS / WORDS / GAP   default 40 / 8 / 2000 / 15
#   HEAVY_FRAC / HEAVY_MULT          default 0.7 / 3.0  (the byte-skew load)
#   SKEW_FRAC                        default 0           (0 = let placement decide)
set -u

A=${1:?usage: run_2x2.sh pin|bytes off|on}
B=${2:?usage: run_2x2.sh pin|bytes off|on}
REPS=${REPS:-3}
PROGRAMS=${PROGRAMS:-40}
TURNS=${TURNS:-8}
WORDS=${WORDS:-2000}
GAP=${GAP:-15}
HEAVY_FRAC=${HEAVY_FRAC:-0.7}
HEAVY_MULT=${HEAVY_MULT:-3.0}
SKEW_FRAC=${SKEW_FRAC:-0}
EXEC_ROOT=${EXEC_ROOT:-<server-dir>}
SSH_HOST=${SSH_HOST:-<ascend-host>}
SSH_PORT=${SSH_PORT:-<ssh-port>}
SSH_USER=${SSH_USER:-root}
LOCAL=${LOCAL:-0}
if [ "$LOCAL" = "1" ]; then
  SSH=""            # run straight on the engine host
else
  SSH="ssh -o BatchMode=yes -o StrictHostKeyChecking=no -p $SSH_PORT $SSH_USER@$SSH_HOST"
fi
OUT=${OUT:-data/m2x2.jsonl}

case "$A" in
  pin)   CAP=1.0  ;;
  bytes) CAP=0.85 ;;
  *) echo "A must be pin|bytes"; exit 2 ;;
esac
case "$B" in
  off) BAL=off  ;;
  on)  BAL=skew ;;
  *) echo "B must be off|on"; exit 2 ;;
esac

mkdir -p "$(dirname "$OUT")"
for rep in $(seq 1 "$REPS"); do
  label="2x2-${A}-${B}-r${rep}"
  if [ "$LOCAL" = "1" ]; then
    pkill -9 -f 'uvicorn executor.app' >/dev/null 2>&1; sleep 3
    ( cd "$EXEC_ROOT" && env EXECUTOR_BACKENDS='a=http://127.0.0.1:8110,b=http://127.0.0.1:8111' \
        EXECUTOR_PLACEMENT=affinity EXECUTOR_PLACEMENT_CAP=$CAP \
        EXECUTOR_BALANCE=$BAL EXECUTOR_MODE=kv EXECUTOR_MIGRATE_CARRIER=recompute \
        EXECUTOR_MIGRATE_FALLBACK=recompute \
        EXECUTOR_BALANCE_HOT=0.75 EXECUTOR_BALANCE_COLD=0.55 EXECUTOR_BALANCE_INTERVAL=10 \
        nohup python3 -m uvicorn executor.app:app --host 127.0.0.1 --port 8100 \
        > <server-dir>${A}_${B}_r${rep}.log 2>&1 & )
  else
  $SSH "pkill -9 -f 'uvicorn executor.app'; sleep 3" >/dev/null 2>&1
  $SSH -f "cd $EXEC_ROOT && exec env \
      EXECUTOR_BACKENDS='a=http://127.0.0.1:8110,b=http://127.0.0.1:8111' \
      EXECUTOR_PLACEMENT=affinity EXECUTOR_PLACEMENT_CAP=$CAP \
      EXECUTOR_BALANCE=$BAL EXECUTOR_MODE=kv EXECUTOR_MIGRATE_CARRIER=recompute \
      EXECUTOR_MIGRATE_FALLBACK=recompute \
      EXECUTOR_BALANCE_HOT=0.75 EXECUTOR_BALANCE_COLD=0.55 EXECUTOR_BALANCE_INTERVAL=10 \
      python3 -m uvicorn executor.app:app --host 127.0.0.1 --port 8100 \
      > <server-dir>${A}_${B}_r${rep}.log 2>&1"
  fi
  sleep 12
  http=$(curl -s --max-time 5 http://127.0.0.1:8100/healthz || true)
  echo "[$label] healthz=$(echo "$http" | head -c 120)"
  python3 tests/m6_bench.py --label "$label" --arm "${A}-${B}" \
      --programs "$PROGRAMS" --turns "$TURNS" --prefix-words "$WORDS" \
      --gap-s "$GAP" --skew-frac "$SKEW_FRAC" \
      --heavy-frac "$HEAVY_FRAC" --heavy-mult "$HEAVY_MULT" >> "$OUT"
  tail -1 "$OUT"
done