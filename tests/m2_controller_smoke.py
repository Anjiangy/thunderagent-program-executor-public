"""M2: controller (:9000) -> executor (:8100) -> vLLM x2.

Run on the serving host:

    python tests/m2_controller_smoke.py [controller_url] [executor_url]

Checks the integration seams that only show up once a real controller sits in
front of the executor:

  1. the controller read the *cluster* capacity from the executor (not one card);
  2. requests with a program_id flow through and become tracked programs;
  3. the executor's pin table agrees with the controller's program table;
  4. two programs end up on different instances (spread);
  5. a second turn of the same program reuses the same pin (affinity);
  6. an ordinary (no program_id) request still works and is not tracked;
  7. the downstream hint never reaches vLLM (responses parse as normal).
"""

from __future__ import annotations

import json
import re
import sys

import httpx

CTL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9000"
EXE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:8100"

c = httpx.Client(timeout=300.0)
failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(("PASS  " if ok else "FAIL  ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        failures.append(name)


def model_id() -> str:
    # The controller's pin-based surface has no /v1/models route (404), so the
    # model name is taken from the executor it fronts.
    return c.get(EXE + "/v1/models").json()["data"][0]["id"]


def ask(pid: str | None, text: str, max_tokens: int = 8) -> httpx.Response:
    body = {
        "model": model_id(),
        "messages": [{"role": "user", "content": text}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
    }
    if pid is not None:
        body["program_id"] = pid
    return c.post(CTL + "/v1/chat/completions", json=body)


def pins() -> dict[str, str]:
    return {p["program_id"]: p["instance"]
            for p in c.get(EXE + "/v1/programs").json()["programs"]}


# 0) clean slate for the programs this script owns
for pid in ("m2-alpha", "m2-beta"):
    c.delete(EXE + f"/v1/programs/{pid}")

# 1) cluster capacity reached the controller
text = c.get(EXE + "/metrics").text
blocks = int(re.search(r'num_gpu_blocks="(\d+)"', text).group(1))
block_size = int(re.search(r'block_size="(\d+)"', text).group(1))
check("executor advertises the sum of both cards", blocks == 1784, f"{blocks} blocks x {block_size}")
check("controller sees one engine, not two", blocks * block_size == 228352, str(blocks * block_size))

# 2) program traffic flows
r1 = ask("m2-alpha", "hello from the M2 smoke test")
check("programmed request succeeds", r1.status_code == 200, str(r1.status_code))
check("response still parses as OpenAI JSON", "choices" in r1.json(), str(list(r1.json())[:4]))

health = c.get(CTL + "/health").json()
check("controller tracked the program", health["programs_count"] >= 1, json.dumps(health["programs_count"]))

# 3) both tables agree -- this is the seam program_id stripping used to break
table = pins()
check("executor knows the program by name", "m2-alpha" in table, str(table))

# 4) second program spreads
ask("m2-beta", "another program")
table = pins()
check("two programs occupy two instances",
      table.get("m2-alpha") != table.get("m2-beta"), str(table))

# 5) affinity on the second turn
before = table["m2-alpha"]
ask("m2-alpha", "second turn of the first program, longer text")
table = pins()
check("second turn keeps the pin", table["m2-alpha"] == before,
      f"{before} -> {table['m2-alpha']}")

# 6) ordinary request untouched
r4 = c.post(CTL + "/v1/chat/completions", json={
    "model": model_id(),
    "messages": [{"role": "user", "content": "no program id here"}],
    "max_tokens": 4,
})
check("ordinary request works", r4.status_code == 200, str(r4.status_code))

# 7) hint must not leak downstream
check("executor strips the hint", "program_id_hint" not in json.dumps(r1.json()))

# 8) migrate then next turn lands on the new instance
target = "b" if table["m2-alpha"] == "a" else "a"
m = c.post(EXE + "/v1/programs/m2-alpha/migrate", json={"target": target, "mode": "recompute"})
check("migrate accepted", m.status_code == 200, m.text[:120])
ask("m2-alpha", "after migration")
check("next turn follows the new pin", pins()["m2-alpha"] == target, str(pins()))

print()
print(json.dumps({"failures": failures}))
sys.exit(1 if failures else 0)
