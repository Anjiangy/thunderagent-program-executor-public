"""M6 workload: does moving work beat re-materialising it?

Four runs, two per arm. Each run pins 28 of 40 programs onto one card, then runs
eight rounds with a 15 s gap standing in for a tool call. The arms differ only in
how a move is carried.
"""
from __future__ import annotations

import csv
import pathlib
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
with (ROOT / "data" / "m6_workload.csv").open(encoding="utf-8-sig") as fh:
    rows = list(csv.DictReader(fh))

arms = {"recompute": ("recompute handoff (re-prefill on target)", "#e07b39", "s"),
        "transfer": ("transfer handoff (pull blocks from source)", "#1f5fbf", "o")}
order = list(arms)

hit = {a: [float(r["hit_rate"]) * 100 for r in rows if r["arm"] == a] for a in arms}
rec = {a: [float(r["recompute_tokens"]) / 1000 for r in rows if r["arm"] == a] for a in arms}
tpm = {a: [float(r["turns_per_minute"]) for r in rows if r["arm"] == a] for a in arms}

fig, axes = plt.subplots(1, 3, figsize=(13.6, 4.3))

def scatter(ax, data, ylabel, title, ylim, fmt="{:.1f}"):
    for i, arm in enumerate(order):
        _, color, marker = arms[arm]
        ys = data[arm]
        ax.scatter([i - 0.07 + 0.07 * j for j in range(len(ys))], ys,
                   s=70, color=color, marker=marker, zorder=3)
        med = statistics.median(ys)
        ax.plot([i - 0.26, i + 0.26], [med, med], color=color, lw=2.6, zorder=2)
        ax.text(i + 0.30, med, f"median {fmt.format(med)}", va="center", fontsize=9, color=color)
    ax.set_xticks(range(len(order)))
    ax.set_xticklabels([arms[a][0].split(" (")[0] for a in order], fontsize=9)
    ax.set_xlim(-0.6, 1.75)
    ax.set_ylim(*ylim)
    ax.set_title(title, fontsize=10)
    ax.set_ylabel(ylabel, fontsize=9)
    ax.grid(axis="y", alpha=0.25)

scatter(axes[0], hit, "prefix-cache hit rate (%)",
        "A crowded card thrashes either way", (0, 24), "{:.1f}%")
scatter(axes[1], rec, "tokens recomputed (thousands)",
        "Every turn is recomputed in both arms", (0, 900), "{:.0f}k")
scatter(axes[2], tpm, "turns per minute",
        "Moving costs throughput, buys nothing here", (60, 100), "{:.1f}")

fig.suptitle("40 programs x 8 rounds x 15 s gaps, 28 of 40 pinned to one card, 1 move per run", fontsize=11)
fig.tight_layout()
out = ROOT / "data" / "m6_workload.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
