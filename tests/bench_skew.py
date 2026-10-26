"""Skewed, heavy-tailed program load: does rebalancing pay when one card fills first?

Unlike bench3.py this can *force* a landing (executor /v1/programs/{pid}/seed), so a
deliberately bad initial placement can be created and the admission layer's response
measured. Prefixes are unique per program, so a hit can only come from that program's
own earlier turn.

Metrics come from vLLM /metrics deltas: recompute_tokens = dq - dh is the real work
redone because the prefix was no longer resident.
"""
import argparse
import json
import random
import statistics
import threading
import time

import httpx

MODEL = "<model-path>"
WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi "
         "omicron pi rho sigma tau upsilon phi chi psi omega").split()


def metrics(bases):
    q = h = running = 0.0
    kv = 0.0
    for base in bases:
        try:
            text = httpx.get(base + "/metrics", timeout=10).text
        except Exception:
            continue
        for line in text.splitlines():
            if line.startswith("vllm:prefix_cache_queries_total"):
                q += float(line.rsplit(" ", 1)[1])
            elif line.startswith("vllm:prefix_cache_hits_total"):
                h += float(line.rsplit(" ", 1)[1])
            elif line.startswith("vllm:num_requests_running"):
                running += float(line.rsplit(" ", 1)[1])
            elif line.startswith("vllm:kv_cache_usage_perc"):
                kv = max(kv, float(line.rsplit(" ", 1)[1]))
    return {"q": q, "h": h, "running": running, "kv": kv}


def one_turn(client, exe, pid, ctx, max_tokens):
    t0 = time.perf_counter()
    r = client.post(exe + "/v1/chat/completions", json={
        "model": MODEL,
        "messages": [{"role": "user", "content": ctx}],
        "max_tokens": max_tokens,
        "temperature": 0.0,
        "stream": True,
        "program_id": pid,
    })
    if r.status_code != 200:
        raise RuntimeError(f"{pid} HTTP {r.status_code}")
    first = None
    for line in r.iter_lines():
        if line.startswith("data:") and first is None:
            first = time.perf_counter()
            break
    return (first or time.perf_counter()) - t0


class PauseSampler(threading.Thread):
    def __init__(self, ctl_url):
        super().__init__(daemon=True)
        self.ctl_url = ctl_url
        self.peak = 0
        self.samples = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            try:
                payload = httpx.get(self.ctl_url + "/health", timeout=5).json()
                self.peak = max(self.peak, int(payload.get("paused_count") or 0))
                self.samples += 1
            except Exception:
                pass
            self._stop.wait(2.0)

    def stop(self):
        self._stop.set()
        self.join(timeout=5)


def seed(client, exe, pid, target, prompt):
    try:
        return client.post(exe + f"/v1/programs/{pid}/seed",
                           json={"target": target, "prompt": prompt, "model": MODEL}).status_code
    except Exception as exc:  # noqa: BLE001
        return f"err:{exc!r}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", required=True)
    ap.add_argument("--exe", default="http://127.0.0.1:8100")
    ap.add_argument("--ctl-url", default=None, help="controller base url for pause sampling")
    ap.add_argument("--backends", default="http://127.0.0.1:8022,http://127.0.0.1:8023")
    ap.add_argument("--programs", type=int, default=40)
    ap.add_argument("--turns", type=int, default=3)
    ap.add_argument("--prefix-words", type=int, default=2500)
    ap.add_argument("--heavy-frac", type=float, default=0.25)
    ap.add_argument("--heavy-mult", type=float, default=1.5)
    ap.add_argument("--skew", type=float, default=0.0, help="fraction of programs forced onto --seed-target")
    ap.add_argument("--seed-target", default="a")
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    bases = [b for b in args.backends.split(",") if b]
    rng = random.Random(f"{args.label}:{args.seed}")
    heavy_n = int(round(args.programs * args.heavy_frac))
    progs = []
    for i in range(args.programs):
        words = args.prefix_words
        if i < heavy_n:
            words = int(args.prefix_words * args.heavy_mult)
        progs.append((f"p-{args.label}-{i}", " ".join(rng.choice(WORDS) for _ in range(words))))

    skew_n = int(round(args.programs * args.skew))
    sampler = PauseSampler(args.ctl_url) if args.ctl_url else None
    before = metrics(bases)
    t0 = time.perf_counter()
    seeded = 0
    with httpx.Client(timeout=float("inf")) as client:
        for pid, prefix in progs[:skew_n]:
            if seed(client, args.exe, pid, args.seed_target, prefix) == 200:
                seeded += 1

        results = {}
        errors = []

        def run(pid, prefix):
            ctx = prefix
            ttfts = []
            try:
                for turn in range(args.turns):
                    ctx += f"\n\nturn {turn}: answer in one short sentence."
                    ttfts.append(one_turn(client, args.exe, pid, ctx, args.max_tokens))
                results[pid] = ttfts
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=run, args=(pid, pre)) for pid, pre in progs]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    wall = time.perf_counter() - t0
    if sampler:
        sampler.stop()
    after = metrics(bases)
    dq = after["q"] - before["q"]
    dh = after["h"] - before["h"]
    ttfts = [v for vs in results.values() for v in vs]
    row = {
        "arm": args.label,
        "programs": args.programs,
        "turns": args.turns,
        "heavy_frac": args.heavy_frac,
        "heavy_mult": args.heavy_mult,
        "skew": args.skew,
        "seeded": seeded,
        "done": len(results),
        "failed": len(progs) - len(results),
        "wall_s": round(wall, 1),
        "turns_per_min": round(len(ttfts) / wall * 60, 1) if wall else None,
        "hit_rate": round(dh / dq, 4) if dq else None,
        "recompute_tokens": int(dq - dh),
        "ttft_p50_s": round(statistics.median(ttfts), 2) if ttfts else None,
        "ttft_p95_s": round(sorted(ttfts)[max(int(len(ttfts) * 0.95) - 1, 0)], 2) if ttfts else None,
        "paused_peak_sampled": sampler.peak if sampler else None,
        "errors": errors[:2],
    }
    print("RESULT " + json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
