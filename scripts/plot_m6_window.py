"""What an artificial migration window is worth: balancer off vs on, near the wall."""
from __future__ import annotations
import csv, pathlib, statistics
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
rows = list(csv.DictReader((ROOT / "data" / "m6_pressure_window.csv").open(encoding="utf-8-sig")))
groups = ["no balancer", "balancer + KV pull", "balancer + recompute"]
color = {"no balancer": "#6b7280", "balancer + KV pull": "#1f5fbf", "balancer + recompute": "#e07b39"}

def vals(arm, key, scale=1.0):
    return [float(r[key]) * scale for r in rows if r["arm"] == arm]

fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.4))
panels = [
    ("hit_rate", 100.0, "Prefix-cache hit rate (%)", 0, 62),
    ("recompute_tokens", 1.0, "Tokens recomputed", 0, 520000),
    ("lat_p50_s", 1.0, "Median turn latency (s)", 0.0, 1.7),
]
for ax, (key, scale, ylabel, lo, hi) in zip(axes, panels):
    for i, arm in enumerate(groups):
        ys = vals(arm, key, scale)
        xs = [i - 0.10 + 0.07 * j for j in range(len(ys))]
        ax.scatter(xs, ys, s=55, color=color[arm], zorder=3)
        med = statistics.median(ys)
        ax.plot([i - 0.26, i + 0.26], [med, med], color=color[arm], lw=2.4)
        ax.text(i + 0.30, med, f"{med:,.0f}" if scale == 1.0 and key != "lat_p50_s" else f"{med:.2f}",
                va="center", fontsize=8.5, color=color[arm])
    ax.set_xticks(range(len(groups)))
    ax.set_xticklabels(["no\nbalancer", "balancer\n+ KV pull", "balancer\n+ recompute"], fontsize=8.5)
    ax.set_ylim(lo, hi)
    ax.set_ylabel(ylabel, fontsize=9.5)
    ax.grid(axis="y", alpha=0.25)

fig.suptitle("Hot card at 0.95 of capacity (40 programs, 900-word prefixes): the balancer is worth 43 points", fontsize=11)
fig.tight_layout()
out = ROOT / "data" / "m6_pressure_window.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
