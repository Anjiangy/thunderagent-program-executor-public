"""M4 probe: does mode=kv actually leave the target's prefix cache warm?

Two programs get identical treatment except for the migration mode. Both build
their context on the same instance, then both move to an instance that has never
seen their prefix. One is warmed first (mode=kv), one is not (mode=recompute).
Then each takes one more turn on its new home and we read that instance's own
counters around the single request.

If kv migration is real, the warmed program's first post-move request is mostly a
cache hit; the recompute one pays a full prefill.

The control arm is deliberately kept clean: the programs are pinned with a tiny
dummy prompt first, so their real prefix is only ever written on the source
instance. Without that, a control program that once ran on the target would show
hits from its own old cache and the comparison would be meaningless.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import urllib.request

WORDS = (
    "alpha bravo charlie delta echo foxtrot golf hotel india juliet kilo lima "
    "mike november oscar papa quebec romeo sierra tango uniform victor whiskey "
    "xray yankee zulu kernel scheduler block table prefill decode cache prefix "
    "tensor shard pipeline latency throughput capacity eviction admission token"
).split()

Q_RE = re.compile(
    r"^vllm:(prefix_cache_queries_total|prefix_cache_hits_total)\{[^}]*\}\s+([0-9.eE+-]+)", re.M)


def http_json(url: str, payload=None, timeout: float = 900.0):
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"content-type": "application/json"},
                                 method="POST" if payload is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def counters(base_url: str) -> tuple[int, int]:
    with urllib.request.urlopen(base_url.rstrip("/") + "/metrics", timeout=30) as resp:
        text = resp.read().decode()
    totals = {"q": 0.0, "h": 0.0}
    for metric, value in Q_RE.findall(text):
        totals["q" if "queries" in metric else "h"] += float(value)
    return int(totals["q"]), int(totals["h"])


def turn(base: str, model: str, pid: str, prompt: str) -> dict:
    out = http_json(base + "/v1/chat/completions", {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 16,
        "temperature": 0.0,
        "program_id": pid,
    })
    return out.get("usage") or {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8100")
    ap.add_argument("--a", default="http://127.0.0.1:8022")
    ap.add_argument("--b", default="http://127.0.0.1:8023")
    ap.add_argument("--model", default="<model-path>")
    ap.add_argument("--words", type=int, default=2600)
    ap.add_argument("--turns", type=int, default=2, help="real turns built on the source")
    ap.add_argument("--label", default="m4")
    ap.add_argument("--source", default="a")
    args = ap.parse_args()

    inst = {"a": args.a, "b": args.b}
    source = args.source
    target = "b" if source == "a" else "a"
    # The program table outlives a probe run, so every run gets its own ids. A
    # reused id would inherit the previous run's recorded body and warm the wrong
    # bytes - which is exactly how this probe first produced a false negative.
    programs = [("p-kv-" + args.label, "p-kv", "kv"),
                ("p-rc-" + args.label, "p-rc", "recompute")]
    rng = random.Random("prefix::" + args.label)

    print("== phase 0: create pins with a dummy prompt (keeps the real prefix clean) ==")
    for pid, _, _ in programs:
        turn(args.base, args.model, pid, "hello " + pid)
        entry = http_json(f"{args.base}/v1/programs/{pid}")
        if entry["instance"] != source:
            http_json(f"{args.base}/v1/programs/{pid}/migrate",
                      {"target": source, "mode": "recompute", "timeout_s": 120})
        entry = http_json(f"{args.base}/v1/programs/{pid}")
        print(f"  {pid} pinned to {entry['instance']}")

    prefixes = {pid: " ".join(rng.choice(WORDS) for _ in range(args.words))
                for pid, _, _ in programs}
    history = {pid: [] for pid, _, _ in programs}

    print(f"== phase 1: build context on {source} ==")
    for pid, _, _ in programs:
        for turn_no in range(args.turns):
            history[pid].append(f"tool output {turn_no}: {'d' * 200}")
            usage = turn(args.base, args.model, pid, prefixes[pid] + "\n\n" + "\n".join(history[pid]))
            print(f"  {pid} turn {turn_no + 1}: prompt_tokens={usage.get('prompt_tokens')}")
    for pid, _, _ in programs:
        entry = http_json(f"{args.base}/v1/programs/{pid}")
        print(f"  {pid} home={entry['instance']} prompt_tokens≈{entry['prompt_tokens']} "
              f"warm_ready={entry['warm_ready']}")

    print(f"== phase 2: move both from {source} to {target} ==")
    results = {}
    for pid, name, mode in programs:
        before = counters(inst[target])
        resp = http_json(f"{args.base}/v1/programs/{pid}/migrate",
                         {"target": target, "mode": mode, "timeout_s": 180})
        after_move = counters(inst[target])
        history[pid].append("tool output final: " + "e" * 200)
        usage = turn(args.base, args.model, pid, prefixes[pid] + "\n\n" + "\n".join(history[pid]))
        after_turn = counters(inst[target])

        dq_move, dh_move = after_move[0] - before[0], after_move[1] - before[1]
        dq_turn, dh_turn = after_turn[0] - after_move[0], after_turn[1] - after_move[1]
        results[name] = {
            "program_id": pid,
            "mode": mode,
            "warm_ok": (resp.get("warm") or {}).get("ok"),
            "warm_queries": dq_move,
            "warm_hits": dh_move,
            "turn_queries": dq_turn,
            "turn_hits": dh_turn,
            "turn_hit_rate": round(dh_turn / dq_turn, 4) if dq_turn else None,
            "prompt_tokens": usage.get("prompt_tokens"),
        }
        print(f"  {pid} migrate({mode}) ok={resp.get('ok')} warm={(resp.get('warm') or {}).get('ok')}; "
              f"after move dq+{dq_move} dh+{dh_move}; turn dq+{dq_turn} dh+{dh_turn}")

    print("\n== result ==")
    payload = {"label": args.label, "source": source, "target": target,
               "pins": "<see above>", "programs": results}
    print(json.dumps(payload, indent=2))
    kv = results["p-kv"]["turn_hit_rate"]
    rc = results["p-rc"]["turn_hit_rate"]
    if not kv or rc is None:
        print("INCONCLUSIVE: no queries observed")
        return 2
    print(f"first post-move request hit rate: kv={kv:.3f} recompute={rc:.3f}")
    ok = kv > 0.5 and rc < 0.2
    print("PASS" if ok else "FAIL: kv migration did not leave the cache warm")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
