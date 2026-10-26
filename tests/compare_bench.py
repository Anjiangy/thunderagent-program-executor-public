"""Head-to-head bench: ThunderAgent alone vs ThunderAgent + our executor layer.

Both arms run the same controller code, the same two vLLM instances, the same
programs and the same prompts. The only difference is what the controller talks
to:

    arm "ta"   : ThunderAgent --backends 8022,8023        (per-card capacity)
    arm "ours" : ThunderAgent -> executor :8100 -> 8022,8023
                 (cluster capacity + program affinity + warm migration)

So the delta is exactly our layer, not a different scheduler.

    python tests/compare_bench.py --ctl-url http://127.0.0.1:9001 \
        --label ta-n40 --programs 40 --turns 4 --size-skew

Design notes
------------
* `--size-skew` gives 20% of the programs a 3x prefix. Symmetric uniform load
  cannot favour migration (both cards fill up together, so there is never a cold
  card to move to); a heavy tail can, because two large programs landing on one
  card is what leaves the other card idle.
* Prefixes are unique per (label, program), so an arm can never inherit the
  other arm's cached blocks.
* A sampler thread polls the controller every 2s and records the peak number of
  paused programs: that is the throttling cost, not a latency artifact.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import random
import statistics
import sys
import threading
import time

import httpx

INSTANCES = {"a": "http://127.0.0.1:8022", "b": "http://127.0.0.1:8023"}
WORDS = (
    "ledger orbit canyon velvet harbor ember lattice meridian quartz saffron "
    "tundra willow cinder fathom granite hollow ivory juniper kelp lagoon "
    "marble nectar onyx prickle quarry ripple sable thistle umber vellum "
    "wharf xenon yarrow zephyr anvil birch cobalt dune elm fjord glade "
    "heron inlet jetty knoll loam moor nook orchid pine quill reed stone "
    "trellis urn vale wren yew zinnia banner cedar delta flint grove"
).split()


def safe_json(client: httpx.Client, url: str, default=None, timeout: float = 10.0):
    try:
        response = client.get(url, timeout=timeout)
        if response.status_code != 200:
            return default
        return response.json()
    except Exception:  # noqa: BLE001 - optional endpoints differ per arm
        return default


def scrape(client: httpx.Client) -> dict:
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


def reset(client: httpx.Client, ctl: str, exe: str | None) -> None:
    programs = safe_json(client, ctl + "/programs", {}).get("programs") or []
    for p in programs:
        pid = p.get("program_id")
        if not pid:
            continue
        try:
            client.post(ctl + "/programs/release", json={"program_id": pid}, timeout=5.0)
        except Exception:  # noqa: BLE001
            pass
        if exe:
            try:
                client.delete(f"{exe}/v1/programs/{pid}", timeout=5.0)
            except Exception:  # noqa: BLE001
                pass


class PauseSampler(threading.Thread):
    def __init__(self, client: httpx.Client, ctl: str, interval: float = 2.0):
        super().__init__(daemon=True)
        self.client, self.ctl, self.interval = client, ctl, interval
        self.peak_paused = 0
        self.peak_programs = 0
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            health = safe_json(self.client, self.ctl + "/health", {}, timeout=5.0)
            self.peak_paused = max(self.peak_paused, int(health.get("paused_count") or 0))
            self.peak_programs = max(self.peak_programs, int(health.get("programs_count") or 0))
            self._stop.wait(self.interval)

    def stop(self) -> None:
        self._stop.set()


def make_prefix(rng: random.Random, words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(words))


def run_program(client: httpx.Client, ctl: str, pid: str, prefix: str, turns: int,
                model: str, rng: random.Random) -> list[dict]:
    history: list[str] = []
    rows: list[dict] = []
    for turn in range(turns):
        prompt = (prefix + "\n\n" + ("\n".join(history) if history else "")
                  + f"\n\nNote {turn}: keep going.")
        t0 = time.time()
        try:
            r = client.post(ctl + "/v1/chat/completions", json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": 16,
                "temperature": 0.0,
                "program_id": pid,
            })
            latency = time.time() - t0
            status = r.status_code
            body = r.json() if status == 200 else {}
        except Exception as exc:  # noqa: BLE001
            rows.append({"turn": turn, "status": -1, "latency": time.time() - t0,
                         "error": f"{type(exc).__name__}"})
            continue
        usage = (body.get("usage") or {}) if status == 200 else {}
        rows.append({"turn": turn, "status": status, "latency": latency,
                     "prompt_tokens": usage.get("prompt_tokens"),
                     "completion_tokens": usage.get("completion_tokens")})
        history.append(f"Note {turn}: " + " ".join(rng.choice(WORDS) for _ in range(60)))
        if status != 200:
            break
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ctl-url", default="http://127.0.0.1:9000")
    ap.add_argument("--exe-url", default="")
    ap.add_argument("--model", default="<model-path>")
    ap.add_argument("--programs", type=int, default=40)
    ap.add_argument("--turns", type=int, default=4)
    ap.add_argument("--prefix-words", type=int, default=2500)
    ap.add_argument("--size-skew", action="store_true",
                    help="give 20%% of programs a 3x prefix (heavy tail)")
    ap.add_argument("--label", default="run")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    client = httpx.Client(timeout=900.0)
    ctl, exe = args.ctl_url, (args.exe_url or None)

    reset(client, ctl, exe)
    before = scrape(client)
    health_before = safe_json(client, ctl + "/health", {})

    rng = random.Random(f"{args.label}:{args.seed}")
    sizes = {}
    for i in range(args.programs):
        heavy = args.size_skew and i % 5 == 0
        sizes[f"{args.label}-p{i}"] = args.prefix_words * (3 if heavy else 1)
    prefixes = {pid: make_prefix(rng, words) for pid, words in sizes.items()}

    sampler = PauseSampler(client, ctl)
    sampler.start()
    t0 = time.time()
    with cf.ThreadPoolExecutor(max_workers=args.programs) as pool:
        futures = {pool.submit(run_program, client, ctl, pid, prefix, args.turns, args.model, random.Random(pid)): pid
                   for pid, prefix in prefixes.items()}
        per_program = {futures[f]: f.result() for f in cf.as_completed(futures)}
    wall = time.time() - t0
    sampler.stop()

    after = scrape(client)
    health_after = safe_json(client, ctl + "/health", {})

    rows = [r for rs in per_program.values() for r in rs]
    ok = [r for r in rows if r.get("status") == 200]
    latencies = sorted(r["latency"] for r in ok)

    def delta(metric: str) -> float:
        return sum(after.get(f"{n}:{metric}", 0) - before.get(f"{n}:{metric}", 0)
                   for n in INSTANCES)

    dq, dh = delta("prefix_cache_queries_total"), delta("prefix_cache_hits_total")

    result = {
        "label": args.label,
        "arm_ctl": ctl,
        "programs": args.programs,
        "turns": args.turns,
        "size_skew": args.size_skew,
        "requests": len(rows),
        "ok": len(ok),
        "failed": len(rows) - len(ok),
        "wall_seconds": round(wall, 1),
        "turns_per_minute": round(len(ok) / wall * 60, 1) if wall else None,
        "latency_p50_s": round(statistics.median(latencies), 2) if latencies else None,
        "latency_p95_s": round(latencies[int(len(latencies) * 0.95) - 1], 2) if latencies else None,
        "latency_max_s": round(latencies[-1], 2) if latencies else None,
        "hit_rate": round(dh / dq, 4) if dq else None,
        "prompt_tokens": sum(r.get("prompt_tokens") or 0 for r in ok),
        "prefix_cache_queries": int(dq),
        "prefix_cache_hits": int(dh),
        "paused_peak_sampled": sampler.peak_paused,
        "programs_peak_sampled": sampler.peak_programs,
        "controller_health_before": {k: health_before.get(k) for k in ("programs_count", "paused_count")},
        "controller_health_after": {k: health_after.get(k) for k in ("programs_count", "paused_count")},
    }
    print(json.dumps(result, indent=2))
    return 0 if result["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
