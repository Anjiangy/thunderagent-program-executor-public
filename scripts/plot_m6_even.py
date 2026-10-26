"""Step 1: even placement, and the capacity wall underneath it.

40 programs spread evenly over two cards, so nothing has to move. The two
carriers are therefore identical by construction - and the hit rate is decided
by whether the resident set fits, which the sweep shows plainly.
"""
from __future__ import annotations

import csv
import pathlib
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
with (ROOT / "data" / "m6_even_placement.csv").open(encoding="utf-8-sig") as fh:
    rows = list(csv.DictReader(fh))

buckets = {}
for r in rows:
    key = int(r["prefix_tokens"])
    buckets.setdefault(key, {"per_card": int(r["per_card_tokens"]),
                             "cap": int(r["card_capacity_tokens"]), "hit": {}, "tpm": {}})
    buckets[key]["hit"].setdefault(r["arm"], []).append(float(r["hit_rate"]) * 100)
    buckets[key]["tpm"].setdefault(r["arm"], []).append(float(r["turns_per_minute"]))

xs = sorted(buckets)
cap = buckets[xs[0]]["cap"]

fig, axes = plt.subplots(1, 2, figsize=(12.4, 4.5))

ax = axes[0]
ax.axhline(87.5, color="#9ca3af", ls="--", lw=1.2)
ax.text(xs[0], 89.5, "8-turn ceiling 87.5% (first turn must miss)", fontsize=8, color="#6b7280")
for arm, color, marker in (("recompute", "#e07b39", "s"), ("transfer", "#1f5fbf", "o")):
    ys = [statistics.median(buckets[x]["hit"][arm]) for x in xs]
    ax.plot(xs, ys, color=color, marker=marker, lw=2, label=f"{arm} carrier")
ax.set_xlabel("prefix tokens per program", fontsize=9)
ax.set_ylabel("prefix-cache hit rate (%)", fontsize=9)
ax.set_title("Even placement: what decides the hit rate is capacity, not the carrier", fontsize=10)
ax.set_ylim(-3, 95)
ax.set_xlim(0, 3400)
ax.set_xticks([0, 1000, 2000, 3000])
ax.grid(alpha=0.25)
ax.legend(frameon=False, fontsize=9, loc="lower left")

ax = axes[1]
width = 0.34
for i, x in enumerate(xs):
    for j, (arm, color, _) in enumerate((("recompute", "#e07b39", None), ("transfer", "#1f5fbf", None))):
        vals = buckets[x]["tpm"][arm]
        ax.bar(i + (j - 0.5) * width, statistics.median(vals), width * 0.92, color=color,
               label=arm if i == 0 else None)
ax.set_xticks(range(len(xs)))
ax.set_xticklabels([f"{x} tok\n({buckets[x]['per_card']/1000:.0f}k/card)" for x in xs], fontsize=8.5)
ax.set_ylabel("turns per minute", fontsize=9)
ax.set_title("Throughput follows the same wall", fontsize=10)
ax.set_ylim(80, 175)
ax.grid(axis="y", alpha=0.25)
ax.legend(frameon=False, fontsize=9)

fig.suptitle("40 programs, even 20/20 placement, 8 turns: the carrier never runs, capacity sets the result", fontsize=11)
fig.tight_layout()
out = ROOT / "data" / "m6_even_placement.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
