#!/usr/bin/env python3
"""Sample pause pressure from both sides to explain the extra pausing (M5 diag).

Writes a CSV: for each tick, our controller's paused/active view, the executor's
rendered /metrics (what the controller scrapes), the raw vLLM /metrics, and the
lag between them.  Run it during a load test; compare `ours` vs the same run with
the bare ThunderAgent arm to see whether the extra pauses come from a stale or
inflated usage reading.

  python3 scripts/diagnose_pause_rate.py --seconds 60 --out /tmp/pause.csv
"""
from __future__ import annotations

import argparse
import csv
import time
import urllib.request

CONTROLLER = "http://127.0.0.1:9000"
EXECUTORS = {"a": "http://127.0.0.1:8101", "b": "http://127.0.0.1:8102"}
VLLM = {"a": "http://127.0.0.1:8022", "b": "http://127.0.0.1:8023"}
KEYS = ("kv_cache_usage_perc", "num_requests_running", "num_requests_waiting")


def get(url: str, timeout: float = 2.0) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def value(text: str, key: str) -> float:
    total = 0.0
    for line in text.splitlines():
        if line.startswith("vllm:" + key):
            total += float(line.rsplit(" ", 1)[-1])
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--out", default="/tmp/pause.csv")
    args = ap.parse_args()

    fields = ["t", "controller_paused", "controller_programs"]
    for name in EXECUTORS:
        fields += [f"exec_{name}_{k}" for k in KEYS] + [f"vllm_{name}_{k}" for k in KEYS]
    rows = []
    t0 = time.time()
    while time.time() - t0 < args.seconds:
        row = {"t": round(time.time() - t0, 2)}
        try:
            health = get(CONTROLLER + "/health")
            row["controller_paused"] = health.split('"paused_count":')[1].split(",")[0].strip()
            row["controller_programs"] = health.split('"programs_count":')[1].split(",")[0].strip()
        except Exception:
            row["controller_paused"] = row["controller_programs"] = "?"
        for name, base in EXECUTORS.items():
            try:
                text = get(base + "/metrics")
                for key in KEYS:
                    row[f"exec_{name}_{key}"] = round(value(text, key), 4)
            except Exception:
                for key in KEYS:
                    row[f"exec_{name}_{key}"] = "?"
        for name, base in VLLM.items():
            try:
                text = get(base + "/metrics")
                for key in KEYS:
                    row[f"vllm_{name}_{key}"] = round(value(text, key), 4)
            except Exception:
                for key in KEYS:
                    row[f"vllm_{name}_{key}"] = "?"
        rows.append(row)
        time.sleep(args.interval)

    with open(args.out, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(f"wrote {args.out} ({len(rows)} samples)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
