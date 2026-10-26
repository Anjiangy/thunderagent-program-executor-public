"""One table, three arms, the same engines on the same pod on the same evening."""
from __future__ import annotations
import csv, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
rows = list(csv.DictReader((ROOT / "data" / "m6_total_table.csv").open(encoding="utf-8-sig")))
even = [r for r in rows if r["placement"] == "even"]

fig, axes = plt.subplots(1, 2, figsize=(11.4, 4.5))
colors = {"bare TA": "#6b7280", "recompute handoff": "#e07b39", "transfer (KV pull)": "#1f5fbf"}
for ax, key, scale, title, ylabel in (
    (axes[0], "hit_rate", 100.0, "Prefix-cache hit rate: the bare router is ahead", "hit rate (%)"),
    (axes[1], "recompute_tokens", 1.0, "Tokens recomputed: same story", "tokens recomputed"),
):
    labels = [r["arm"] for r in even]
    vals = [float(r[key]) * scale for r in even]
    bars = ax.bar(range(len(even)), vals, color=[colors[r["arm"]] for r in even], width=0.55)
    ax.set_xticks(range(len(even)))
    ax.set_xticklabels([l.replace(" (", "\n(") for l in labels], fontsize=8.5)
    ax.set_ylabel(ylabel)
    ax.set_title(title, fontsize=10.5)
    ax.grid(axis="y", alpha=0.25)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height(), f"{v:.1f}" if scale == 100 else f"{v:,.0f}",
                ha="center", va="bottom", fontsize=9)
    ax.set_ylim(min(vals) * 0.9 if scale == 1 else 74, max(vals) * 1.06 if scale == 1 else 84)

fig.suptitle("40 programs, 8 turns, even placement: no carrier is best, and moving never fires", fontsize=11)
fig.tight_layout()
out = ROOT / "data" / "m6_total_table.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
