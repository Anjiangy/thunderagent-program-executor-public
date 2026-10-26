"""M3: does program affinity actually pay off?

Same chain, one switch:

    client -> controller :9000 -> executor :8100 -> vLLM :8022 | :8023

Arm A: executor placement = affinity    (each program's turns stay on one card)
Arm B: executor placement = roundrobin  (turns alternate between cards)

Everything else -- controller, capacity accounting, prompts, concurrency -- is
identical, so the delta is the value of the pin.

Each program carries its own long prefix (like a distinct agent system prompt)
and grows it turn by turn, which is the shape vLLM's prefix cache rewards.

    python tests/m3_affinity_bench.py --programs 16 --turns 8 --prefix-words 1600 --label A
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import random
import re
import statistics
import sys
import time

import httpx

CTL = "http://127.0.0.1:9000"
EXE = "http://127.0.0.1:8100"
INSTANCES = {"a": "http://127.0.0.1:8022", "b": "http://127.0.0.1:8023"}

WORDS = (
    "ledger orbit canyon velvet harbor ember lattice meridian quartz saffron "
    "tundra willow cinder fathom granite hollow ivory juniper kelp lagoon "
    "marble nectar onyx prickle quarry ripple sable thistle umber vellum "
    "wharf xenon yarrow zephyr anvil birch cobalt dune elm fjord glade "
    "heron inlet jetty knoll loam moor nook orchid pine quill reed stone "
    "trellis urn vale wren yew zinnia banner cedar delta flint grove"
).split()


def scrape(client: httpx.Client) -> dict:
    """Sum prefix-cache counters across every label combination on both cards."""
    out = {}
    for name, base in INSTANCES.items():
        try:
            text = client.get(base + "/metrics", timeout=10.0).text
        except Exception:  # noqa: BLE001
            continue
        for metric in ("prefix_cache_queries_total", "prefix_cache_hits_total",
                       "prompt_tokens_total", "generation_tokens_total"):
            total = 0.0
            for line in text.splitlines():
                if line.startswith("vllm:" + metric):
                    parts = line.rsplit(" ", 1)
                    if len(parts) == 2:
                        try:
                            total += float(parts[1])
                        except ValueError:
                            pass
            out[f"{name}:{metric}"] = total
    return out


def reset(client: httpx.Client) -> None:
    """Drop program state on both layers so each arm starts clean."""
    try:
        programs = client.get(EXE + "/v1/programs", timeout=10.0).json()["programs"]
    except Exception:  # noqa: BLE001
        programs = []
    for p in programs:
        pid = p["program_id"]
        try:
            client.delete(f"{EXE}/v1/programs/{pid}", timeout=5.0)
            client.post(CTL + "/programs/release", json={"program_id": pid}, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass


def make_prefix(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def run_program(client: httpx.Client, pid: str, prefix: str, turns: int,
                model: str) -> list[dict]:
    history: list[str] = []
    rows: list[dict] = []
    for turn in range(turns):
        prompt = (
            prefix
            + "\n\n"
            + ("\n".join(history) if history else "")
            + f"\n\nNote {turn}: keep going."
        )
        t0 = time.time()
        r = client.post(base + "/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16,
            "temperature": 0.0,
            "program_id": pid,
        })
        latency = time.time() - t0
        if r.status_code != 200:
            rows.append({"turn": turn, "status": r.status_code, "latency": latency})
            continue
        usage = r.json().get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        rows.append({
            "turn": turn,
            "status": 200,
            "latency": latency,
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": details.get("cached_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
        })
        # Grow the context the way an agent's tool output would.
        history.append(f"Note {turn}: " + " ".join(rng.choice(WORDS) for _ in range(60)))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--programs", type=int, default=16)
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--prefix-words", type=int, default=1600)
    ap.add_argument("--label", default="run")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--target", default="controller", choices=["controller", "executor"],
                    help="send turns through the controller or straight to the executor")
    args = ap.parse_args()

    client = httpx.Client(timeout=900.0)
    model = client.get(EXE + "/v1/models").json()["data"][0]["id"]
    base = CTL if args.target == "controller" else EXE

    reset(client)
    before = scrape(client)
    health_before = client.get(CTL + "/health").json()

    placement = client.get(EXE + "/healthz").json().get("placement")
    rng = random.Random(args.seed)
    # Unique prefixes per arm so arm B cannot inherit arm A's cached KV.
    prefixes = {f"{args.label}-p{i}": make_prefix(rng, args.prefix_words)
                for i in range(args.programs)}

    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.programs) as pool:
        futures = {
            pool.submit(run_program, client, base, pid, prefix, args.turns, model): pid
            for pid, prefix in prefixes.items()
        }
        per_program = {futures[f]: f.result() for f in cf.as_completed(futures)}
    wall = time.time() - t0

    after = scrape(client)
    health_after = client.get(CTL + "/health").json()
    exe_health = client.get(EXE + "/healthz").json()
    programs_now = client.get(EXE + "/v1/programs", timeout=10.0).json()["programs"]
    migrations = sum(p.get("migrations") or 0 for p in programs_now)
    warmed = sum(p.get("warmed_tokens") or 0 for p in programs_now)

    rows = [r for rs in per_program.values() for r in rs]
    ok = [r for r in rows if r.get("status") == 200]
    latencies = sorted(r["latency"] for r in ok)
    details_seen = any(r.get("cached_tokens") is not None for r in ok)
    cached = sum(r.get("cached_tokens") or 0 for r in ok)
    prompt_tokens = sum(r.get("prompt_tokens") or 0 for r in ok)

    def delta(metric: str) -> float:
        return sum(after.get(f"{n}:{metric}", 0) - before.get(f"{n}:{metric}", 0)
                   for n in INSTANCES)

    dq, dh = delta("prefix_cache_queries_total"), delta("prefix_cache_hits_total")

    pins = {}
    for p in client.get(EXE + "/v1/programs", timeout=10.0).json()["programs"]:
        pins.setdefault(p["instance"], 0)
        pins[p["instance"]] += 1

    result = {
        "label": args.label,
        "placement": placement,
        "programs": args.programs,
        "turns": args.turns,
        "requests": len(rows),
        "ok": len(ok),
        "failed": len(rows) - len(ok),
        "wall_seconds": round(wall, 1),
        "turns_per_minute": round(len(ok) / wall * 60, 1) if wall else None,
        "latency_p50_s": round(statistics.median(latencies), 3) if latencies else None,
        "latency_p95_s": round(latencies[int(len(latencies) * 0.95) - 1], 3) if latencies else None,
        "latency_max_s": round(latencies[-1], 3) if latencies else None,
        "cache_hit_rate_from_counters": round(dh / dq, 4) if dq else None,
        "cache_hit_rate_from_usage": (round(cached / prompt_tokens, 4)
                                       if details_seen and prompt_tokens else None),
        "prompt_tokens": prompt_tokens,
        "cached_tokens": cached,
        "instances_used_by_pins": pins,
        "executor_balance": exe_health.get("balance"),
        "executor_balance_moves": exe_health.get("balance_moves"),
        "executor_migrations": migrations,
        "executor_warmed_tokens": warmed,
        "controller_programs": health_after.get("programs_count"),
        "controller_paused_peak_sample": health_after.get("paused_count"),
        "controller_programs_before": health_before.get("programs_count"),
    }
    print(json.dumps(result, indent=2))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
