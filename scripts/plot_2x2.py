"""2x2: hit rate and median latency for (placement signal) x (migration on/off)."""
from __future__ import annotations
import json, os, pathlib, statistics
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
src = pathlib.Path(os.environ.get("M2X2", ROOT / "data" / "m2x2.jsonl"))
rows = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
cells = [("pin", "off"), ("pin", "on"), ("bytes", "off"), ("bytes", "on")]
names = {(0, 0): "pin counts\nno migration", (0, 1): "pin counts\nmigration on",
         (1, 0): "bytes\nno migration", (1, 1): "bytes\nmigration on"}
colors = ["#e07b39", "#b91c1c", "#1f5fbf", "#3f8f4f"]

def vals(a, b, key, scale=1.0):
    out = [r[key] * scale for r in rows if r["arm"] == f"{a}-{b}"]
    return out

fig, axes = plt.subplots(1, 3, figsize=(15.5, 4.6))
for ax, key, scale, title, ylabel in (
    (axes[0], "hit_rate", 100.0, "Prefix-cache hit rate", "hit rate (%)"),
    (axes[1], "lat_p50_s", 1.0, "Median turn latency", "p50 (s)"),
    (axes[2], "turns_per_minute", 1.0, "Throughput", "turns/min"),
):
    for idx, (a, b) in enumerate(cells):
        ys = vals(a, b, key, scale)
        if not ys:
            continue
        xs = [idx - 0.12 + 0.08 * j for j in range(len(ys))]
        ax.scatter(xs, ys, s=70, color=colors[idx], zorder=3)
        ax.plot([idx - 0.26, idx + 0.26], [statistics.median(ys)] * 2, color=colors[idx], lw=2.6)
        ax.text(idx + 0.3, statistics.median(ys), f"{statistics.median(ys):.2f}", va="center",
                fontsize=8.5, color=colors[idx])
    ax.set_xticks(range(len(cells)))
    ax.set_xticklabels([names[(1 if a == "bytes" else 0, 1 if b == "on" else 0)] for a, b in cells], fontsize=8)
    ax.set_title(title, fontsize=10.5)
    ax.set_ylabel(ylabel)
    ax.grid(axis="y", alpha=0.25)

fig.suptitle("2x2: does byte-aware placement help, and does migration help at all?", fontsize=11.5)
fig.tight_layout()
out = ROOT / "data" / "m2x2.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
