"""Three-arm bench: affinity / affinity+warm / affinity+transfer.

Same load, same seed, same machine. Only the placement+migration policy changes.

  arm a: EXECUTOR_PLACEMENT=affinity, EXECUTOR_MODE=recompute   (pure affinity)
  arm b: EXECUTOR_PLACEMENT=affinity, EXECUTOR_MODE=kv          (warm-replay = route A)
  arm c: EXECUTOR_PLACEMENT=affinity, EXECUTOR_MODE=kv, transfer (route B, needs MooncakeStore)

The arms are switched server-side; this script only drives load and measures.

Why not turns/minute: on agent-shaped load the wall clock is dominated by tool
execution, so tpm hides the effect. We report, per turn:
  * recompute tokens  = d(prefix_cache_queries) - d(prefix_cache_hits)   <- the real cost
  * ttft p50/p95
  * kv usage peak on each card

usage:
  python tests/three_arm_bench.py --label a --programs 24 --turns 8 --prefix-words 2500
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics
import threading
import time

import httpx

WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
         "nu xi omicron pi rho sigma tau upsilon phi chi psi omega").split()


def make_prefix(rng: random.Random, n_words: int) -> str:
    return " ".join(rng.choice(WORDS) for _ in range(n_words))


def metrics_sums(base: str) -> dict[str, float]:
    out = {"q": 0.0, "h": 0.0, "running": 0.0, "kv": 0.0}
    try:
        txt = httpx.get(f"{base}/metrics", timeout=10).text
    except Exception:
        return out
    for line in txt.splitlines():
        if line.startswith("vllm:prefix_cache_queries_total"):
            out["q"] += float(line.rsplit(" ", 1)[1])
        elif line.startswith("vllm:prefix_cache_hits_total"):
            out["h"] += float(line.rsplit(" ", 1)[1])
        elif line.startswith("vllm:num_requests_running"):
            out["running"] += float(line.rsplit(" ", 1)[1])
        elif line.startswith("vllm:kv_cache_usage_perc"):
            out["kv"] = max(out["kv"], float(line.rsplit(" ", 1)[1]))
    return out


class PauseSampler(threading.Thread):
    def __init__(self, ctl: str, period: float = 2.0):
        super().__init__(daemon=True)
        self.ctl, self.period, self.peak, self._stop = ctl, period, 0, threading.Event()

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                h = httpx.get(f"{self.ctl}/health", timeout=5).json()
                self.peak = max(self.peak, int(h.get("paused_programs", 0)))
            except Exception:
                pass
            self._stop.wait(self.period)

    def stop(self) -> int:
        self._stop.set()
        return self.peak


def run_program(client: httpx.Client, exe: str, pid: str, prefix: str,
                turns: int, max_tokens: int) -> list[float]:
    ttfts: list[float] = []
    ctx = prefix
    for t in range(turns):
        ctx += f"\n\nturn {t}: continue the story in one short sentence."
        t0 = time.perf_counter()
        r = client.post(f"{exe}/v1/completions", json={
            "model": "local", "prompt": ctx, "max_tokens": max_tokens,
            "temperature": 0.0, "stream": True,
            "program_id": pid,
        })
        if r.status_code != 200:
            raise RuntimeError(f"{pid} turn {t}: HTTP {r.status_code}")
        first = None
        for line in r.iter_lines():
            if line.startswith("data:") and first is None:
                first = time.perf_counter()
                break
        ttfts.append((first or time.perf_counter()) - t0)
        if len(ctx) > 120_000:
            ctx = prefix + "\n\n(compacted)"
    return ttfts


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--ctl", default="http://127.0.0.1:9000")
    ap.add_argument("--exe", default="http://127.0.0.1:8100")
    ap.add_argument("--backends", default="http://127.0.0.1:8022,http://127.0.0.1:8023")
    ap.add_argument("--programs", type=int, default=24)
    ap.add_argument("--turns", type=int, default=8)
    ap.add_argument("--prefix-words", type=int, default=2500)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--arrive-every", type=float, default=0.0,
                    help="0 = launch all at once; >0 = steady arrival (seconds)")
    ap.add_argument("--seed", type=int, default=None)
    args = ap.parse_args()

    seed = args.seed if args.seed is not None else int(time.time())
    rng = random.Random(f"{args.label}:{seed}")
    bases = [b for b in args.backends.split(",") if b]

    before = {b: metrics_sums(b) for b in bases}
    sampler = PauseSampler(args.ctl)
    sampler.start()

    started = time.perf_counter()
    programs = [(f"p-{args.label}-{i}", make_prefix(rng, args.prefix_words))
                for i in range(args.programs)]
    results: dict[str, list[float]] = {}
    with httpx.Client(timeout=900) as client:
        threads = []
        for idx, (pid, prefix) in enumerate(programs):
            if args.arrive_every > 0 and idx:
                time.sleep(args.arrive_every)
            th = threading.Thread(target=lambda p=pid, s=prefix: results.__setitem__(
                p, run_program(client, args.exe, p, s, args.turns, args.max_tokens)))
            th.start()
            threads.append(th)
        for th in threads:
            th.join()
    wall = time.perf_counter() - started
    paused_peak = sampler.stop()

    after = {b: metrics_sums(b) for b in bases}
    dq = sum(after[b]["q"] - before[b]["q"] for b in bases)
    dh = sum(after[b]["h"] - before[b]["h"] for b in bases)
    ttft = [v for vs in results.values() for v in vs]

    row = {
        "arm": args.label, "seed": seed, "programs": args.programs, "turns": args.turns,
        "ok": len(results), "failed": args.programs - len(results),
        "wall_seconds": round(wall, 1),
        "turns_per_minute": round(len(ttft) / wall * 60, 1),
        "hit_rate": round(dh / dq, 4) if dq else None,
        "recompute_tokens": int(dq - dh),
        "ttft_p50_s": round(statistics.median(ttft), 2) if ttft else None,
        "ttft_p95_s": round(sorted(ttft)[int(len(ttft) * 0.95) - 1], 2) if ttft else None,
        "paused_peak_sampled": paused_peak,
    }
    out = pathlib.Path("data") / "three_arm_results.jsonl"
    out.parent.mkdir(exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
    print(json.dumps(row, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
