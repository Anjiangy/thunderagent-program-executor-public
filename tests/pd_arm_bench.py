"""One arm of the disaggregation cost table.

    recompute : the decode engine is asked directly, so it prefills the prompt itself
    transfer  : the request goes through the P/D proxy, so the decode engine receives
                blocks the prefill engine just computed

Each run sends the same long prefix twice; the second send is the one that matters
(its prefix is resident either way -- the difference is *who* paid to build it).
Every arm gets its own salt so the two arms cannot feed each other's prefix cache.
"""
from __future__ import annotations

import argparse
import json
import time

import httpx

WORDS = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu "
         "nu xi omicron pi rho sigma tau upsilon phi chi psi omega").split()


def make_prefix(n_words: int, salt: str) -> str:
    seed = sum(ord(c) for c in salt)
    return " ".join(WORDS[(i * 7 + seed) % len(WORDS)] + str((i + seed) % 97) for i in range(n_words))


def counters(base: str) -> dict:
    text = httpx.get(f"{base}/metrics", timeout=20).text
    out = {"q": 0.0, "h": 0.0}
    for line in text.splitlines():
        if line.startswith("#") or " " not in line:
            continue
        name, _, value = line.rpartition(" ")
        try:
            v = float(value)
        except ValueError:
            continue
        if "prefix_cache_queries_total" in name:
            out["q"] += v
        elif "prefix_cache_hits_total" in name:
            out["h"] += v
    return out


def send(url: str, model: str, prompt: str, max_tokens: int) -> dict:
    body = {"model": model, "prompt": prompt, "max_tokens": max_tokens, "temperature": 0.0}
    t0 = time.time()
    with httpx.Client(timeout=900.0) as client:
        resp = client.post(f"{url}/v1/completions", json=body)
    wall = time.time() - t0
    resp.raise_for_status()
    payload = resp.json()
    return {"wall_s": round(wall, 3), "usage": payload.get("usage", {}), "text": payload["choices"][0]["text"][:24]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["recompute", "transfer"], required=True)
    ap.add_argument("--salt", default="")
    ap.add_argument("--proxy", default="http://127.0.0.1:8104")
    ap.add_argument("--decode", default="http://127.0.0.1:8103")
    ap.add_argument("--prefill", default="http://127.0.0.1:8102")
    ap.add_argument("--model", default="<model-path>")
    ap.add_argument("--words", type=int, default=900)
    ap.add_argument("--max-tokens", type=int, default=8)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    url = args.proxy if args.arm == "transfer" else args.decode
    salt = args.salt or args.arm
    prefix = make_prefix(args.words, salt)
    rows = []
    for turn in (1, 2):
        p0, d0 = counters(args.prefill), counters(args.decode)
        r = send(url, args.model, prefix + f"\n\nTurn {turn}: name a colour.", args.max_tokens)
        p1, d1 = counters(args.prefill), counters(args.decode)
        rows.append({
            "arm": args.arm, "turn": turn, "wall_s": r["wall_s"],
            "prompt_tokens": r["usage"].get("prompt_tokens"),
            "prefill_computed": (p1["q"] - p0["q"]) - (p1["h"] - p0["h"]),
            "prefill_reused": p1["h"] - p0["h"],
            "decode_computed": (d1["q"] - d0["q"]) - (d1["h"] - d0["h"]),
            "decode_reused": d1["h"] - d0["h"],
            "text": r["text"],
        })
        print(json.dumps(rows[-1], ensure_ascii=False))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
