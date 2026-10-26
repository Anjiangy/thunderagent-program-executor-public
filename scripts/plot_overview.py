"""One page: what the path-A executor stack has actually shown so far."""
from __future__ import annotations
import csv, pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = pathlib.Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def rows(name):
    with (DATA / name).open(encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


aff = rows("m3_affinity.csv")
kv = rows("m4_kv_results.csv")
cmp_ = rows("m4_compare.csv")

FA, NA = "#1f5fbf", "#e07b39"
fig, axes = plt.subplots(1, 3, figsize=(15.4, 4.6))

ax = axes[0]
for placement, color, marker, label in (("affinity", FA, "o", "program affinity"),
                                       ("roundrobin", NA, "s", "round-robin (naive)")):
    xs = [int(r["programs"]) for r in aff if r["placement"] == placement]
    ys = [float(r["cache_hit_rate"]) * 100 for r in aff if r["placement"] == placement]
    ax.plot(xs, ys, marker=marker, color=color, lw=2.2, label=label)
ax.set_xlabel("concurrent programs")
ax.set_ylabel("prefix-cache hit rate (%)")
ax.set_title("Affinity only pays once one card overflows")
ax.set_ylim(40, 95)
ax.grid(alpha=0.25)
ax.legend(frameon=False, fontsize=9, loc="lower left")

ax = axes[1]
labels, vals = [], []
for arm in ("kv", "recompute"):
    for r in kv:
        if r["mode"] != arm:
            continue
        labels.append(f"{list(r.values())[0]}\n{arm}")
        vals.append(float(r["hit_rate"]) * 100)
colors = [FA if "kv" in l and "recompute" not in l else NA for l in labels]
ax.bar(range(len(vals)), vals, color=colors, width=0.62)
for i, v in enumerate(vals):
    ax.text(i, v + 1.5, f"{v:.1f}%", ha="center", fontsize=9)
ax.set_xticks(range(len(vals)))
ax.set_xticklabels(labels, fontsize=7.5)
ax.set_ylim(0, 112)
ax.set_ylabel("hit rate of the first turn after the move (%)")
ax.set_title("A warm migration keeps the moved program cached")

ax = axes[2]
ns = [int(r["n"]) for r in cmp_]
for arm, color, marker, col in (("ta", "#6b7280", "s", "turns_per_minute"),
                                ("ours", FA, "o", "turns_per_minute")):
    pts = [(int(r["n"]), float(r[col]), float(r["hit_rate"]) * 100)
           for r in cmp_ if r.get("arm") == arm and r.get(col) not in ("", None)]
    if not pts:
        continue
    ax.plot([p[0] for p in pts], [p[1] for p in pts], marker=marker, color=color,
            lw=2.2, label=("bare ThunderAgent" if arm == "ta" else "executor + controller"))
ax.set_xlabel("concurrent programs")
ax.set_ylabel("turns per minute")
ax.set_title("Against the bare router we are level, not ahead")
ax.grid(alpha=0.25)
ax.legend(frameon=False, fontsize=9)


# A line that stops early reads as "still fast"; say out loud that it stopped.
_parked = [r for r in cmp_ if r.get("arm") == "ours" and r.get(cmp_ and "turns_per_minute") in ("", None)]
if _parked:
    _x = int(_parked[0]["n"])
    _ax = axes[2]
    _lo, _hi = _ax.get_ylim()
    _ax.axvline(_x, color="#b91c1c", ls=":", lw=1.4)
    _ax.annotate("ours: parked,\ndid not finish", xy=(_x, _hi - (_hi - _lo) * 0.12),
                 xytext=(_x - 6.0, _hi - (_hi - _lo) * 0.30), color="#b91c1c", fontsize=8.5,
                 arrowprops=dict(arrowstyle="->", color="#b91c1c", lw=1.2))

fig.suptitle("Route A executor: three measurements that survive scrutiny", fontsize=13)
fig.tight_layout()
out = DATA / "m5_overview.png"
fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size, "bytes")
