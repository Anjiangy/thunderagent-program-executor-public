#!/bin/bash
# Six runs: three per carrier. Each run restarts the executor so the program
# table starts empty, but the engines keep running.
cd <server-dir> || exit 1
mkdir -p results logs
for arm in recompute transfer; do
  for r in 1 2 3; do
    label="m6_${arm}_r${r}"
    echo "=== $label $(date +%H:%M:%S)"
    pkill -f 'uvicorn executor.app' 2>/dev/null
    sleep 3
    EXECUTOR_BACKENDS='a=http://127.0.0.1:8110,b=http://127.0.0.1:8111' \
    EXECUTOR_PORT=8100 EXECUTOR_PLACEMENT=affinity EXECUTOR_BALANCE=skew \
    EXECUTOR_BALANCE_INTERVAL=5.0 EXECUTOR_MODE=kv EXECUTOR_MIGRATE_CARRIER=$arm \
    EXECUTOR_SCRAPE_INTERVAL=2.0 \
    nohup /usr/local/python3.12.13/bin/python3 -m uvicorn executor.app:app \
        --host 127.0.0.1 --port 8100 >> <server-dir>$label.log 2>&1 &
    sleep 10
    /usr/local/python3.12.13/bin/python3 tests/m6_bench.py --label "$label" --arm "$arm" \
        >> <server-dir>$label.log 2>&1
    echo "=== done $label"
  done
done
echo ALL_DONE
