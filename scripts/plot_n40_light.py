"""N=40, 3 turns, 1200-word prefixes: bare ThunderAgent vs executor+controller."""
import csv, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
with (ROOT / "data" / "m5_n40_light.csv").open(encoding="utf-8-sig") as fh:
    rows = {r["arm"]: r for r in csv.DictReader(fh)}

labels = ["bare ThunderAgent", "executor + controller"]
colors = ["#6b7280", "#1f5fbf"]
fig, axes = plt.subplots(1, 3, figsize=(12.4, 4.0))

panels = [
    ("hit_rate", "prefix-cache hit rate (%)", 100.0, "{:.2f}%"),
    ("turns_per_min", "turns per minute", 1.0, "{:.1f}"),
    ("recompute_tokens", "recomputed tokens", 1.0, "{:,.0f}"),
]
for ax, (key, title, scale, fmt) in zip(axes, panels):
    vals = [float(rows[a][key]) * scale for a in ("ta", "ours")]
    bars = ax.bar(labels, vals, color=colors, width=0.55)
    for b, v in zip(bars, vals):
        ax.text(b.get_x() + b.get_width() / 2, v, fmt.format(v), ha="center", va="bottom", fontsize=9)
    ax.set_title(title)
    ax.set_ylim(0, max(vals) * 1.18)
    ax.tick_params(axis="x", labelsize=8.5)
    ax.grid(axis="y", alpha=0.25)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)

fig.suptitle("N=40, 3 turns, 1200-word prefixes, 229K KV: level, not ahead", fontsize=11.5)
fig.tight_layout()
out = ROOT / "data" / "m5_n40_light.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
