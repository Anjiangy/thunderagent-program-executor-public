"""M6 workload: many programs, long waits, one card crowded.

Per run: 40 programs with unique prefixes land by affinity, then most of them are
forced onto card `a` with a recompute move. The rounds then run with a gap that
stands in for a tool call, and the executor's balancer decides on its own whether
to move programs back. The only difference between the two arms is how a move is
carried: `EXECUTOR_MIGRATE_CARRIER=recompute|transfer`.
"""
from __future__ import annotations

import argparse
import concurrent.futures as futures
import os
import json
import random
import statistics
import time
import urllib.error
import urllib.request

EXE = os.environ.get("BENCH_EXE", "http://127.0.0.1:8100")
SKIP_CONTROL = os.environ.get("BENCH_NO_CONTROL") == "1"
BACKENDS = {"a": "http://127.0.0.1:8110", "b": "http://127.0.0.1:8111"}
MODEL = os.environ.get("BENCH_MODEL", "Qwen2.5-14B-Instruct")
WORDS = ["alpha", "bravo", "charlie", "delta", "echo", "foxtrot", "golf", "hotel",
         "india", "juliet", "kilo", "lima", "mike", "november", "oscar", "papa",
         "quebec", "romeo", "sierra", "tango", "uniform", "victor", "whiskey"]


def http(url: str, payload: dict | None = None, timeout: float = 1200.0):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"content-type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    started = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        inst = r.headers.get("x-executor-instance")
    return raw, time.time() - started, inst


def metrics(port: int) -> dict:
    import re
    raw, _, _ = http(f"http://127.0.0.1:{port}/metrics", None, 30)
    out = {}
    for name in ("prefix_cache_queries_total", "prefix_cache_hits_total"):
        total = 0.0
        for m in re.finditer(rf"vllm:{name}\{{[^}}]*\}}\s+([0-9.eE+-]+)", raw):
            total += float(m.group(1))
        out[name] = total
    return out


def cluster_metrics() -> dict:
    acc = {"prefix_cache_queries_total": 0.0, "prefix_cache_hits_total": 0.0}
    for port in (8110, 8111):
        got = metrics(port)
        for k in acc:
            acc[k] += got[k]
    return acc


def turn(pid: str, prefix: str, extra: str, max_tokens: int = 2):
    """One turn, tolerating the executor's migration window.

    While a program is being moved the executor answers 503 with Retry-After;
    that is expected, not a failure, so the client waits it out. Anything else
    is counted as a failed turn instead of killing the whole run.
    """
    payload = {"model": MODEL,
               "messages": [{"role": "user", "content": prefix + "\n\n" + extra}],
               "max_tokens": max_tokens, "temperature": 0, "program_id": pid}
    for _ in range(30):
        try:
            _, elapsed, _ = http(f"{EXE}/v1/chat/completions", payload)
            return elapsed
        except urllib.error.HTTPError as exc:
            if exc.code == 503:
                time.sleep(1.0)
                continue
            return None
        except Exception:  # noqa: BLE001 - a dropped turn is data, not a crash
            return None
    return None


def make_prefix(rng: random.Random, words: int) -> str:
    """A distinct, deterministic word salad so no two programs share a prefix."""
    return " ".join(rng.choice(WORDS) for _ in range(words))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--programs", type=int, default=40)
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--prefix-words", type=int, default=2500)
    ap.add_argument("--gap-s", type=float, default=15.0)
    ap.add_argument("--skew-frac", type=float, default=0.7)
    ap.add_argument("--heavy-frac", type=float, default=0.0,
                    help="fraction of programs that get --heavy-mult times the prefix")
    ap.add_argument("--heavy-mult", type=float, default=3.0)
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args()

    rng = random.Random(f"m6:{args.label}:{args.arm}")
    HEAVY_IDS = []
    if args.heavy_frac > 0:
        want = int(round(args.programs * args.heavy_frac))
        order = sorted((f"p{k}" for k in range(args.programs)),
                       key=lambda k: random.Random(f"heavy:{k}").random())
        HEAVY_IDS = order[:want]

    words = {}
    for k in range(args.programs):
        pid = f"p{k}"
        base = args.prefix_words * (args.heavy_mult if pid in HEAVY_IDS else 1.0)
        words[pid] = max(16, int(round(base)))

    prefixes = {pid: make_prefix(random.Random(f"{args.label}:{pid}"), n)
                for pid, n in words.items()}
    heavy = list(prefixes)[: int(args.programs * args.skew_frac)]

    lat: list[float] = []
    failures = 0
    started = time.time()
    before = cluster_metrics()

    with futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        opening = list(pool.map(lambda pid: turn(pid, prefixes[pid], "opening"), prefixes))
    opened = sum(1 for v in opening if v is not None)

    moved = 0
    for pid in ([] if SKIP_CONTROL else heavy):
        try:
            raw, _, _ = http(f"{EXE}/v1/programs/{pid}/migrate",
                             {"target": "a", "mode": "recompute", "timeout_s": 300})
            if json.loads(raw).get("ok"):
                moved += 1
        except Exception:  # noqa: BLE001 - a failed pre-move just leaves it where it landed
            pass

    for t in range(1, args.turns):
        with futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            results = list(pool.map(lambda pid: turn(pid, prefixes[pid], f"turn t{t}"), prefixes))
        lat.extend(v for v in results if v is not None)
        failures += sum(1 for v in results if v is None)
        if t < args.turns - 1:
            time.sleep(args.gap_s)

    after = cluster_metrics()
    wall = time.time() - started
    dq = after["prefix_cache_queries_total"] - before["prefix_cache_queries_total"]
    dh = after["prefix_cache_hits_total"] - before["prefix_cache_hits_total"]
    try:
        health = json.loads(http(f"{EXE}/health", None, 30)[0])
    except Exception:  # noqa: BLE001 - a foreign front door may only expose /healthz
        health = json.loads(http(f"{EXE}/healthz", None, 30)[0])
    try:
        progs = json.loads(http(f"{EXE}/v1/programs", None, 30)[0])
    except Exception:  # noqa: BLE001 - bare ThunderAgent has no program table endpoint
        progs = {"programs": []}
    prog_list = progs.get("programs", []) if isinstance(progs, dict) else progs
    prog_list = [p for p in prog_list if isinstance(p, dict)]

    row = {
        "label": args.label, "arm": args.arm, "programs": args.programs,
        "turns": args.turns, "gap_s": args.gap_s, "prefix_words": args.prefix_words,
        "forced_to_a": moved, "wall_s": round(wall, 1),
        "requests": len(lat), "failures": failures,
        "opening_ok": opened,
        "turns_per_minute": round(len(lat) / wall * 60, 1),
        "hit_rate": round(dh / dq, 4) if dq else None,
        "recompute_tokens": int(dq - dh),
        "lat_p50_s": round(statistics.median(lat), 3) if lat else None,
        "lat_p95_s": round(sorted(lat)[int(len(lat) * 0.95) - 1], 3) if lat else None,
        "balance_moves": health.get("balance_moves"),
        "program_migrations": sum(p.get("migrations", 0) for p in prog_list),
        "carrier": None,
    }
    print(json.dumps(row, ensure_ascii=False))
    with open(f"<server-dir>{args.label}.json", "w", encoding="utf-8") as fh:
        json.dump(row, fh, ensure_ascii=False, indent=1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
