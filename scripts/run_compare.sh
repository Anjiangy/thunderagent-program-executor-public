#!/bin/bash
# Run the two arms at the same program counts, one JSON per (arm, N).
#   arm ta   : upstream ThunderAgent alone      :9001 -> vLLM 8022/8023
#   arm ours : ThunderAgent -> executor :8100   :9000 -> vLLM 8022/8023
# Usage: scripts/run_compare.sh "24 32 40" 4 2500 skew
set -u
N_LIST="${1:-24 32 40}"
TURNS="${2:-4}"
WORDS="${3:-2500}"
SKEW="${4:-skew}"
OUT=<server-dir>
EXE_DIR=<server-dir>
SEED=$(date +%s)
mkdir -p "$OUT"
skew_flag=""
[ "$SKEW" = "skew" ] && skew_flag="--size-skew"

clean() {
  pkill -9 -f 'scripts/ta_launch.py' 2>/dev/null
  pkill -9 -f 'thunderagent_controller_mod' 2>/dev/null
  pkill -9 -f 'uvicorn executor' 2>/dev/null
  pkill -9 -f 'compare_bench.py' 2>/dev/null
  sleep 2
}

wait_ok() {  # full url, tries
  for _ in $(seq 1 "$2"); do
    c=$(curl -s -m 2 -o /dev/null -w '%{http_code}' "$1" 2>/dev/null)
    [ "$c" = "200" ] && return 0
    sleep 1
  done
  return 1
}

progs() { curl -s -m 3 "$1/health" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("programs_count",-1))' 2>/dev/null || echo -1; }

start_ta() {
  clean
  ( cd "$EXE_DIR" && PYTHONPATH=<server-dir> nohup python3 scripts/ta_launch.py \
      --backends http://127.0.0.1:8022,http://127.0.0.1:8023 --port 9001 \
      > <server-dir> 2>&1 & )
  wait_ok http://127.0.0.1:9001/health 25 || { echo "ta arm failed to start"; return 1; }
}

start_ours() {
  clean
  ( cd "$EXE_DIR" && EXECUTOR_BACKENDS='a=http://127.0.0.1:8022,b=http://127.0.0.1:8023' \
      EXECUTOR_PLACEMENT=affinity EXECUTOR_BALANCE=skew \
      nohup python3 -m uvicorn executor.app:app --host 127.0.0.1 --port 8100 \
      > <server-dir> 2>&1 & )
  wait_ok http://127.0.0.1:8100/healthz 25 || { echo "executor failed to start"; return 1; }
  ( cd <server-dir> && PYTHONPATH=<server-dir>:<server-dir> nohup python3 -m thunderagent_controller_mod \
      --endpoint http://127.0.0.1:8100 --listen 0.0.0.0:9000 --enable --scrape-interval 2.0 \
      > <server-dir> 2>&1 & )
  wait_ok http://127.0.0.1:9000/health 25 || { echo "controller failed to start"; return 1; }
}

for N in $N_LIST; do
  for ARM in ta ours; do
    if [ "$ARM" = "ta" ]; then
      start_ta || exit 3
      CTL=http://127.0.0.1:9001
    else
      start_ours || exit 3
      CTL=http://127.0.0.1:9000
    fi
    before=$(progs "$CTL")
    echo "=== $ARM N=$N progs_before=$before"
    if [ "$before" != "0" ]; then echo "DIRTY controller, abort"; exit 2; fi
    if [ "$ARM" = "ta" ]; then
      timeout 2400 python3 "$EXE_DIR/tests/compare_bench.py" --ctl-url "$CTL" \
        --programs "$N" --turns "$TURNS" --prefix-words "$WORDS" $skew_flag \
        --label "$ARM-n$N" --seed "$SEED" > "$OUT/$ARM-n$N.json" 2> "$OUT/$ARM-n$N.err"
    else
      timeout 2400 python3 "$EXE_DIR/tests/compare_bench.py" --ctl-url "$CTL" \
        --exe-url http://127.0.0.1:8100 \
        --programs "$N" --turns "$TURNS" --prefix-words "$WORDS" $skew_flag \
        --label "$ARM-n$N" --seed "$SEED" > "$OUT/$ARM-n$N.json" 2> "$OUT/$ARM-n$N.err"
    fi
    echo "exit=$? -> $OUT/$ARM-n$N.json"
    python3 -c "
import json,sys
try:
    d=json.load(open('$OUT/$ARM-n$N.json'))
    print('  tpm=%.1f hit=%.3f p50=%.2f ok=%d/%d paused=%d wall=%.0fs' % (d['turns_per_minute'],d['hit_rate'],d['latency_p50_s'],d['ok'],d['requests'],d['paused_peak_sampled'],d['wall_seconds']))
except Exception as e:
    print('  (no result:', e, ')')
"
  done
done
clean
echo "=== done"


