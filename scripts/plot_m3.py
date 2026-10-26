"""Draw the M3 dose-response figure from data/m3_affinity.csv."""
from __future__ import annotations

import csv
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = pathlib.Path(__file__).resolve().parent
rows = list(csv.DictReader((HERE.parent / "data" / "m3_affinity.csv").open(encoding="utf-8-sig")))

def series(placement: str, key: str) -> list[tuple[int, float]]:
    out = [(int(r["programs"]), float(r[key])) for r in rows if r["placement"] == placement]
    return sorted(out)

fig, axes = plt.subplots(1, 2, figsize=(11, 4.2))

ax = axes[0]
for placement, style, label in (
    ("affinity", "o-", "program affinity"),
    ("roundrobin", "s--", "round-robin (naive)"),
):
    pts = series(placement, "cache_hit_rate")
    ax.plot([p[0] for p in pts], [p[1] * 100 for p in pts], style, label=label, linewidth=2)
ax.axhline(83.3, color="#999", linestyle=":", linewidth=1)
ax.text(16.2, 84.5, "ceiling with this prompt shape (~83%: first turns must miss)", fontsize=7, color="#666")
ax.set_xlabel("concurrent programs")
ax.set_ylabel("prefix cache hit rate (%)")
ax.set_title("Affinity only pays once the naive working set exceeds one card")
ax.set_xticks([16, 32, 40])
ax.set_ylim(45, 95)
ax.grid(alpha=0.3)
ax.legend()

ax = axes[1]
for placement, style, label in (
    ("affinity", "o-", "program affinity"),
    ("roundrobin", "s--", "round-robin (naive)"),
):
    pts = series(placement, "turns_per_minute")
    ax.plot([p[0] for p in pts], [p[1] for p in pts], style, label=label, linewidth=2)
ax.set_xlabel("concurrent programs")
ax.set_ylabel("turns per minute")
ax.set_title("Same load, same controller: throughput")
ax.set_xticks([16, 32, 40])
ax.grid(alpha=0.3)
ax.legend()

for ax in axes:
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

fig.tight_layout()
out = HERE.parent / "data" / "m3_affinity.png"
fig.savefig(out, dpi=160)
print(out, out.stat().st_size)
