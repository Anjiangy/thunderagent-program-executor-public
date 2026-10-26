"""Plot the ta-vs-ours comparison (data/m4_compare.csv)."""
import csv
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
rows = list(csv.DictReader((ROOT / "data" / "m4_compare.csv").open(encoding="utf-8-sig")))

def series(arm):
    out = []
    for r in rows:
        if r["arm"] != arm:
            continue
        out.append(
            (
                int(r["n"]),
                float(r["turns_per_minute"]) if r["turns_per_minute"] else None,
                float(r["hit_rate"]) if r["hit_rate"] else None,
                r["completed"] == "1",
            )
        )
    return sorted(out)

fig, axes = plt.subplots(1, 2, figsize=(11.5, 4.4))
for ax, idx, ylabel, title in (
    (axes[0], 1, "turns per minute", "Throughput"),
    (axes[1], 2, "prefix cache hit rate", "Cache locality"),
):
    for arm, marker, label in (
        ("ta", "o", "bare ThunderAgent (per-card accounting)"),
        ("ours", "s", "controller + executor (cluster accounting)"),
    ):
        pts = series(arm)
        xs = [p[0] for p in pts if p[idx] is not None]
        ys = [p[idx] for p in pts if p[idx] is not None]
        ax.plot(xs, ys, marker=marker, label=label, linewidth=2, markersize=7)
        missed = [p[0] for p in pts if p[idx] is None]
        if missed:
            ax.scatter(missed, [0.0] * len(missed), marker="x", s=60,
                       color="#c0392b", zorder=5,
                       label="ours: parked, did not finish")
    ax.set_xlabel("concurrent programs")
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(alpha=0.3)
    ax.set_xticks(sorted({int(r["n"]) for r in rows}))
axes[0].set_ylim(0, 380)
axes[1].set_ylim(0, 0.85)
axes[0].legend(frameon=False, fontsize=8, loc="lower left")
fig.suptitle("Same load, same machine: cluster accounting buys nothing until one card overflows", fontsize=11)
fig.tight_layout()
out = ROOT / "data" / "m4_compare.png"
fig.savefig(out, dpi=140)
print("wrote", out, out.stat().st_size, "bytes")
