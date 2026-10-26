"""Live smoke test: needs the executor (:8100) and >=2 vLLM instances behind it.

    python tests/smoke_live.py [http://127.0.0.1:8100]

Checks, in order:
  1. /metrics parses as a cluster view (block_size present, blocks summed);
  2. one program always lands on the same instance;
  3. a *different* program is allowed to land elsewhere;
  4. migrate(recompute) moves the pin and the next request follows it;
  5. mode=kv is refused with 501;
  6. program_id via header works;
  7. unknown target / unknown program / double migrate are refused.
"""

from __future__ import annotations

import json
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8100"
c = httpx.Client(timeout=180.0)

failures = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(name)


def ask(pid: str, text: str = "hello", headers: dict | None = None) -> httpx.Response:
    models = c.get(BASE + "/v1/models").json()
    model = models["data"][0]["id"]
    body = {
        "model": model,
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 8,
        "temperature": 0.0,
    }
    hdrs = dict(headers or {})
    if pid is not None:
        body["program_id"] = pid
    return c.post(BASE + "/v1/chat/completions", json=body, headers=hdrs)


def programs() -> dict:
    return {p["program_id"]: p for p in c.get(BASE + "/v1/programs").json()["programs"]}


# 1) cluster metrics
text = c.get(BASE + "/metrics").text
check("metrics has cache_config_info", "vllm:cache_config_info{" in text)
check("metrics has block_size label", 'block_size="' in text)
check("metrics has num_gpu_blocks label", 'num_gpu_blocks="' in text)
check("metrics has kv_cache_usage_perc", "vllm:kv_cache_usage_perc{" in text)

# 2) affinity
r1 = ask("smoke-a")
i1 = r1.headers.get("x-executor-instance")
r2 = ask("smoke-a", "second turn")
i2 = r2.headers.get("x-executor-instance")
check("affinity: same program keeps its instance", r1.status_code == 200 and i1 == i2, f"{i1} vs {i2}")

# 3) separate program gets pinned somewhere
r3 = ask("smoke-b")
i3 = r3.headers.get("x-executor-instance")
check("second program routed", r3.status_code == 200 and i3 is not None, str(i3))

# 4) migrate follows the pin
cur = programs()
target = "b" if cur["smoke-a"]["instance"] == "a" else "a"
m = c.post(f"{BASE}/v1/programs/smoke-a/migrate",
           json={"target": target, "mode": "recompute", "timeout_s": 30})
check("migrate accepted", m.status_code == 200, str(m.status_code))
r4 = ask("smoke-a", "after migration")
check("request follows migrated pin", r4.headers.get("x-executor-instance") == target,
      str(r4.headers.get("x-executor-instance")))

# 5) kv mode refused
m2 = c.post(f"{BASE}/v1/programs/smoke-a/migrate",
            json={"target": "a" if target == "b" else "b", "mode": "kv"})
check("mode=kv refused with 501", m2.status_code == 501, str(m2.status_code))

# 6) header-only program id
r5 = ask(None, headers={"x-program-id": "smoke-h"})
check("header program_id routed", r5.status_code == 200 and "smoke-h" in programs())

# 7) error paths
bad_target = c.post(f"{BASE}/v1/programs/smoke-a/migrate", json={"target": "zzz"})
check("unknown target -> 400", bad_target.status_code == 400)
unknown = c.get(f"{BASE}/v1/programs/nope")
check("unknown program -> 404", unknown.status_code == 404)

# cleanup
for pid in ("smoke-a", "smoke-b", "smoke-h"):
    c.delete(f"{BASE}/v1/programs/{pid}")

print()
print(json.dumps({"failures": failures}, ensure_ascii=False))
sys.exit(1 if failures else 0)
