"""Long-prefix regime only: hit rate and latency against per-card KV load.

Short-query runs (40-word prefixes) live in a different regime: their per-turn
latency is dominated by a different max_tokens budget, so mixing them on a
"load" axis would imply a trend that is not there. They are reported in the
table, not on this curve.
"""
from __future__ import annotations
import csv, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
rows = [r for r in csv.DictReader((ROOT / "data" / "m6_curve.csv").open(encoding="utf-8-sig"))
        if float(r["prefix_words"]) >= 600]

style = {
    "bare TA": ("#6b7280", "o", "-", 2.2),
    "recompute handoff": ("#e07b39", "s", "-", 2.0),
    "transfer handoff": ("#1f5fbf", "^", "-", 2.0),
}

fig, axes = plt.subplots(1, 2, figsize=(11.6, 4.7))
for ax, key, scale, title, ylabel, lo, hi in (
    (axes[0], "hit_rate", 100.0, "Prefix-cache hit rate vs. how full the hot card is", "hit rate (%)", 40, 86),
    (axes[1], "lat_p50_s", 1.0, "Median turn latency vs. the same axis", "p50 (s)", 0.28, 0.78),
):
    for arm, (color, marker, ls, lw) in style.items():
        pts = sorted((float(r["per_card_load"]), float(r[key]) * scale) for r in rows if r["arm"] == arm)
        if not pts:
            continue
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        if len(pts) == 1:
            ax.scatter(xs, ys, color=color, marker=marker, s=95, zorder=3, label=arm)
        else:
            ax.plot(xs, ys, color=color, marker=marker, ls=ls, lw=lw, ms=7.5, label=arm)
    ax.axvline(1.0, color="#9ca3af", ls="--", lw=1.1)
    ax.text(1.015, lo + 0.06 * (hi - lo), "one card full", fontsize=7.5, color="#6b7280", rotation=90)
    ax.set_xlim(0.35, 1.20)
    ax.set_ylim(lo, hi)
    ax.set_xticks([0.44, 0.75, 1.11])
    ax.set_xticklabels(["0.44\n(21k of 47.6k)", "0.75\n(36k)", "1.11\n(53k, over)"], fontsize=8.5)
    ax.set_xlabel("KV demanded by the hot card ÷ its capacity")
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10.5)
    ax.grid(alpha=0.25)

axes[0].legend(frameon=False, fontsize=8.5, loc="lower left")
fig.suptitle("Long prefixes only, same pod and engines: our curves sit below or on par, never above", fontsize=11)
fig.tight_layout()
out = ROOT / "data" / "m6_curve.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
