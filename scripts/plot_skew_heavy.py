"""Skewed + heavy-tailed landing: does the balancer help once one card is over-subscribed?

32 of 40 programs are force-pinned to card a (executor /v1/programs/{pid}/seed), then
each program runs 3 turns with a 1 s gap (a tool-call stand-in). The only difference
between arms is EXECUTOR_BALANCE; the router is restarted before every run because
ThunderAgent-family routers keep program state across runs.
"""
import csv
import pathlib
import statistics

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
with (ROOT / "data" / "m5_skew_heavy.csv").open(encoding="utf-8-sig") as fh:
    rows = list(csv.DictReader(fh))

arms = {"nobal": ("no rebalancing", "#e07b39", "s"), "bal": ("balance=skew (warm handoff)", "#1f5fbf", "o")}
fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.4))

for idx, (key, title, field) in enumerate([
        ("hit", "prefix-cache hit rate (%)", "hit_rate"),
        ("rc", "tokens recomputed (thousands)", "recompute_tokens")]):
    ax = axes[idx]
    for i, (arm, (label, colour, marker)) in enumerate(arms.items()):
        vals = [float(r[field]) * (100 if field == "hit_rate" else 1 / 1000)
                for r in rows if r["arm"] == arm]
        xs = [i - 0.10 + 0.10 * j for j in range(len(vals))]
        ax.scatter(xs, vals, s=70, color=colour, marker=marker, zorder=3, label=label)
        mean = statistics.median(vals)
        ax.plot([i - 0.24, i + 0.24], [mean] * 2, color=colour, lw=2.4, zorder=2)
        ax.text(i + 0.27, mean, f"median {mean:.1f}", va="center", fontsize=9, color=colour)
    ax.set_xticks(range(len(arms)))
    ax.set_xticklabels([arms[a][0] for a in arms], fontsize=8.5)
    ax.set_xlim(-0.5, 1.7)
    ax.set_title(title)
    ax.grid(axis="y", alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

axes[0].set_ylim(58, 68)
axes[1].set_ylim(0, 185)
fig.suptitle("Skewed + heavy landing (32/40 pinned to one card): rebalancing fires, hit rate drops",
             fontsize=11)
fig.tight_layout()
out = ROOT / "data" / "m5_skew_heavy.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
