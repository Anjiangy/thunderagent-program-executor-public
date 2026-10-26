"""Steady-arrival admission test: token-aware cap vs pin-count only."""
from __future__ import annotations
import csv, pathlib, statistics
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
with (ROOT / "data" / "m5_admission.csv").open(encoding="utf-8-sig") as fh:
    rows = list(csv.DictReader(fh))

arms = {"nocap": ("pin counts only (no cap, no migration)", "#e07b39", "s"),
        "tokencap": ("token-aware cap + migration", "#1f5fbf", "o")}
hits = {k: [float(r["hit_rate"]) * 100 for r in rows if r["arm"] == k] for k in arms}
tpm = {k: [float(r["turns_per_minute"]) for r in rows if r["arm"] == k] for k in arms}

fig, axes = plt.subplots(1, 2, figsize=(11.2, 4.4))
ax = axes[0]
for i, (arm, (label, color, marker)) in enumerate(arms.items()):
    ys = hits[arm]
    xs = [i] * len(ys)
    ax.scatter([i - 0.09 + 0.06 * j for j in range(len(ys))], ys, s=64, color=color, marker=marker, label=label)
    ax.plot([i - 0.22, i + 0.22], [statistics.mean(ys)] * 2, color=color, lw=2.4)
    ax.text(i + 0.26, statistics.mean(ys), f"mean {statistics.mean(ys):.1f}%", va="center", fontsize=9, color=color)
ax.set_xticks(range(len(arms)))
ax.set_xticklabels([arms[a][0] for a in arms], fontsize=8)
ax.set_xlim(-0.55, 1.65)
ax.set_ylim(60, 82)
ax.set_ylabel("prefix-cache hit rate (%)")
ax.set_title("New programs keep arriving; one card fills first")
ax.grid(axis="y", alpha=0.25)
ax.legend(frameon=False, fontsize=8, loc="lower right")

ax = axes[1]
for arm, (label, color, marker) in arms.items():
    ys = tpm[arm]
    ax.scatter([list(arms).index(arm)] * len(ys), ys, s=64, color=color, marker=marker)
    ax.plot([list(arms).index(arm) - 0.22, list(arms).index(arm) + 0.22],
            [statistics.mean(ys)] * 2, color=color, lw=2.4)
    ax.text(list(arms).index(arm) + 0.26, statistics.mean(ys), f"{statistics.mean(ys):.1f}",
            va="center", fontsize=9, color=color)
ax.set_xticks(range(len(arms)))
ax.set_xticklabels([arms[a][0] for a in arms], fontsize=8)
ax.set_ylim(120, 165)
ax.set_ylabel("turns per minute")
ax.set_title("Throughput is unchanged (the cap is free)")
ax.grid(axis="y", alpha=0.25)

fig.suptitle("3 runs per arm, same seed, 32 programs arriving every 1.2 s, 25% heavy", fontsize=11)
fig.tight_layout()
out = ROOT / "data" / "m5_admission.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
