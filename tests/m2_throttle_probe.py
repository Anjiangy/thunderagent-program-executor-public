"""M2 throttle probe: does the controller actually pause programs behind the executor?

Fires N programs whose contexts together exceed the cluster's KV capacity and
samples the controller's /health while they run. With a 228352-token cluster and
~9000-token prompts, 30 programs ask for ~270K -> the controller must hold some.

    python tests/m2_throttle_probe.py [n_programs] [chars_per_prompt]
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import sys
import threading
import time

import httpx

N = int(sys.argv[1]) if len(sys.argv) > 1 else 30
CHARS = int(sys.argv[2]) if len(sys.argv) > 2 else 45000
CTL = "http://127.0.0.1:9000"
EXE = "http://127.0.0.1:8100"

c = httpx.Client(timeout=900.0)
model = c.get(EXE + "/v1/models").json()["data"][0]["id"]
filler = ("The quick brown fox jumps over the lazy dog. " * (CHARS // 45))[:CHARS]

stop = threading.Event()
peak = {"paused": 0, "reasoning": 0}


def sampler() -> None:
    while not stop.is_set():
        try:
            h = c.get(CTL + "/health", timeout=5.0).json()
            peak["paused"] = max(peak["paused"], h.get("paused_count", 0))
            peak["reasoning"] = max(peak["reasoning"], h.get("reasoning_count", 0))
        except Exception:  # noqa: BLE001 - sampling is best effort
            pass
        time.sleep(0.4)


def one(i: int) -> tuple[int, float]:
    t0 = time.time()
    r = c.post(CTL + "/v1/chat/completions", json={
        "model": model,
        "messages": [{"role": "user", "content": f"[p{i}] {filler}"}],
        "max_tokens": 1,
        "temperature": 0.0,
        "program_id": f"thr-{i}",
    })
    return r.status_code, time.time() - t0


sampler_thread = threading.Thread(target=sampler, daemon=True)
sampler_thread.start()

t0 = time.time()
with cf.ThreadPoolExecutor(max_workers=N) as pool:
    results = list(pool.map(one, range(N)))
elapsed = time.time() - t0
stop.set()
sampler_thread.join(timeout=2)

codes: dict[int, int] = {}
for status, _ in results:
    codes[status] = codes.get(status, 0) + 1

print(json.dumps({
    "programs": N,
    "chars_per_prompt": CHARS,
    "approx_tokens_each": CHARS // 5,
    "cluster_capacity": 228352,
    "status_codes": codes,
    "wall_seconds": round(elapsed, 1),
    "slowest_request_s": round(max(t for _, t in results), 1),
    "peak_paused": peak["paused"],
    "peak_reasoning": peak["reasoning"],
}, indent=2))

sys.exit(0 if peak["paused"] > 0 and codes.get(200, 0) == N else 1)
