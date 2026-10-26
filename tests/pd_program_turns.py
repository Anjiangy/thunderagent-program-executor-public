"""Program-level KV reuse across two turns in a P/D disaggregated pair.

Question this answers: when a program's second turn arrives, does the decoder
have to prefill it again, or are the blocks already resident on the decode card?

Two arms, same two-card stack, same prompt length:

  reuse  turn 2 = same long prefix + a short new question
  cold   turn 2 = a different long prefix (nothing resident)

For each turn we read both engines' /metrics before and after and report the
deltas, so "the decoder skipped the prefill" is a number, not a story.
"""
from __future__ import annotations

import argparse
import json
import time

import httpx

WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
         "nu xi omicron pi rho sigma tau upsilon phi chi psi omega").split()


def make_prefix(n_words: int, salt: str) -> str:
    out = []
    for i in range(n_words):
        out.append(WORDS[(i * 7 + len(salt)) % len(WORDS)] + str((i + hash(salt) % 1000) % 97))
    return " ".join(out)


def counters(base: str) -> dict:
    text = httpx.get(f"{base}/metrics", timeout=10).text
    out = {"queries": 0.0, "hits": 0.0, "usage": 0.0, "running": 0.0}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, value = line.rpartition(" ")
        try:
            v = float(value)
        except ValueError:
            continue
        if "prefix_cache_queries_total" in name:
            out["queries"] += v
        elif "prefix_cache_hits_total" in name:
            out["hits"] += v
        elif "kv_cache_usage_perc" in name:
            out["usage"] = max(out["usage"], v)
        elif "num_requests_running" in name:
            out["running"] = max(out["running"], v)
    return out


def ask(proxy: str, model: str, prompt: str, max_tokens: int) -> dict:
    body = {"model": model, "prompt": prompt, "max_tokens": max_tokens, "temperature": 0.0}
    t0 = time.time()
    with httpx.Client(timeout=600.0) as client:
        resp = client.post(f"{proxy}/v1/completions", json=body)
    wall = time.time() - t0
    resp.raise_for_status()
    payload = resp.json()
    return {"wall_s": wall, "usage": payload.get("usage", {}), "text": payload["choices"][0]["text"][:40]}


def turn(proxy: str, pre: str, dec: str, model: str, prompt: str, max_tokens: int) -> dict:
    c0p, c0d = counters(pre), counters(dec)
    r = ask(proxy, model, prompt, max_tokens)
    c1p, c1d = counters(pre), counters(dec)
    return {
        "wall_s": round(r["wall_s"], 3),
        "prompt_tokens": r["usage"].get("prompt_tokens"),
        "prefill": {"queries": c1p["queries"] - c0p["queries"], "hits": c1p["hits"] - c0p["hits"]},
        "decode": {"queries": c1d["queries"] - c0d["queries"], "hits": c1d["hits"] - c0d["hits"]},
        "text": r["text"],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:8104")
    ap.add_argument("--prefill", default="http://127.0.0.1:8102")
    ap.add_argument("--decode", default="http://127.0.0.1:8103")
    ap.add_argument("--model", default="<model-path>")
    ap.add_argument("--words", type=int, default=1500)
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--label", default="pd")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    prefix_a = make_prefix(args.words, "a")
    prefix_b = make_prefix(args.words, "b")
    q1 = "\n\nTurn 1: say one word about the text above."
    q2 = "\n\nTurn 2: name a colour."

    rows = []
    for arm in ("reuse", "cold", "reuse2", "cold2"):
        second_prefix = prefix_a if arm.startswith("reuse") else prefix_b
        t1 = turn(args.proxy, args.prefill, args.decode, args.model, prefix_a + q1, args.max_tokens)
        t2 = turn(args.proxy, args.prefill, args.decode, args.model, second_prefix + q2, args.max_tokens)
        rows.append({
            "arm": arm,
            "turn1": t1,
            "turn2": t2,
            "decode_turn2_saved_tokens": t2["decode"]["hits"],
            "decode_turn2_recomputed": max(0.0, t2["decode"]["queries"] - t2["decode"]["hits"]),
        })
        print(json.dumps(rows[-1], ensure_ascii=False))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)

    print("\n| arm | turn2 wall | prompt | prefill q/h | decode q/h | decode reused | decode recomputed |")
    print("|---|---|---|---|---|---|---|")
    for r in rows:
        t2 = r["turn2"]
        print(f"| {r['arm']} | {t2['wall_s']}s | {t2['prompt_tokens']} | "
              f"{t2['prefill']['queries']:.0f}/{t2['prefill']['hits']:.0f} | "
              f"{t2['decode']['queries']:.0f}/{t2['decode']['hits']:.0f} | "
              f"{r['decode_turn2_saved_tokens']:.0f} | {r['decode_turn2_recomputed']:.0f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
