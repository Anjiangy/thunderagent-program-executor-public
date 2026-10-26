"""M6-B probe: does a program moved while idle actually pull its KV?

No /seed route on this build, so placement comes from its own first request:
every program lands on `a` first (tie broken by name). The control program is
then moved to `b` with mode=recompute, which leaves `b` with nothing cached for
it -- that is the cold baseline.
"""
from __future__ import annotations

import json
import random
import sys
import time
import urllib.request

EXE = "http://127.0.0.1:8100"
MODEL = "<model-path>"
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
         "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa"]


def call(path: str, payload: dict | None = None, timeout: float = 1200.0):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(EXE + path, data=data,
                                 headers={"content-type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        inst = r.headers.get("x-executor-instance")
    elapsed = time.time() - started
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"raw": raw[:200]}
    return body, elapsed, inst


def make_prefix(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def turn(pid: str, prefix: str, extra: str, max_tokens: int = 4):
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": prefix + "\n\n" + extra}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "program_id": pid,
    }
    return call("/v1/chat/completions", payload)


def main() -> int:
    rng = random.Random(11)
    moved_prefix = make_prefix(rng, 2000)
    cold_prefix = make_prefix(rng, 2000)

    body, t_moved_cold, inst0 = turn("mv1", moved_prefix, "first turn")
    print(f"[mv1] first turn on {inst0}: {t_moved_cold:.3f}s")

    mig, _, _ = call("/v1/programs/mv1/migrate", {"target": "b", "mode": "kv", "timeout_s": 300})
    warm = mig.get("warm") or {}
    print(f"[mv1] migrate carrier={mig.get('carrier')} ok={warm.get('ok')} "
          f"blocks={warm.get('blocks')} capture_ms={warm.get('capture_ms')} "
          f"kvp_ready={(mig.get('entry') or {}).get('kvp_ready')}")

    body, t_moved, inst1 = turn("mv1", moved_prefix, "second turn after the move")
    print(f"[mv1] first turn after the move on {inst1}: {t_moved:.3f}s")

    _, t_cold_a, inst2 = turn("co1", cold_prefix, "first turn")
    print(f"[co1] first turn on {inst2}: {t_cold_a:.3f}s (cold)")

    mig2, _, _ = call("/v1/programs/co1/migrate", {"target": "b", "mode": "recompute", "timeout_s": 300})
    print(f"[co1] moved to b with mode=recompute, carrier={mig2.get('carrier')}")
    body, t_cold_b, inst3 = turn("co1", cold_prefix, "second turn")
    print(f"[co1] first turn after the move on {inst3}: {t_cold_b:.3f}s (cold on b)")

    if t_cold_b > 0 and t_moved > 0:
        print(f"RESULT moved={t_moved:.3f}s cold_on_b={t_cold_b:.3f}s speedup={t_cold_b / t_moved:.2f}x")
    return 0


if __name__ == "__main__":
    sys.exit(main())
