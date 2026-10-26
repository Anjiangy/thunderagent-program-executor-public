#!/usr/bin/env bash
# Start the program executor in front of N vLLM instances.
#
#   EXECUTOR_BACKENDS="a=http://127.0.0.1:8022,b=http://127.0.0.1:8023" ./run.sh
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

export EXECUTOR_BACKENDS="${EXECUTOR_BACKENDS:-a=http://127.0.0.1:8022,b=http://127.0.0.1:8023}"
export EXECUTOR_PORT="${EXECUTOR_PORT:-8100}"
export EXECUTOR_SCRAPE_INTERVAL="${EXECUTOR_SCRAPE_INTERVAL:-2.0}"
export EXECUTOR_SCRAPE_TIMEOUT="${EXECUTOR_SCRAPE_TIMEOUT:-2.0}"
export EXECUTOR_MIGRATE_TIMEOUT="${EXECUTOR_MIGRATE_TIMEOUT:-60.0}"

echo "executor: backends=${EXECUTOR_BACKENDS} port=${EXECUTOR_PORT}"
exec uvicorn executor.app:app --host 0.0.0.0 --port "${EXECUTOR_PORT}" --log-level info
