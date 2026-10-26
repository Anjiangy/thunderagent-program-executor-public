"""Plot the M4 probe: does a warm migration leave the target's cache warm?

Reads data/m4_kv_results.csv and writes data/m4_kv.png - two bars per run, the
warmed arm and the recompute control, both moving the same program.
"""

from __future__ import annotations

import csv
import pathlib

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load() -> list[dict]:
    with (ROOT / "data" / "m4_kv_results.csv").open(encoding="utf-8-sig") as fh:
        return [row for row in csv.DictReader(fh) if row.get("hit_rate")]


def main() -> int:
    rows = load()
    runs = []
    for row in rows:
        if row["run"] not in runs:
            runs.append(row["run"])
    data = {run: {} for run in runs}
    for row in rows:
        data[row["run"]][row["mode"]] = float(row["hit_rate"])

    fig, ax = plt.subplots(figsize=(7.2, 4.0), dpi=160)
    width = 0.34
    xs = list(range(len(runs)))
    kv = [data[r].get("kv", 0.0) for r in runs]
    rc = [data[r].get("recompute", 0.0) for r in runs]
    ax.bar([x - width / 2 for x in xs], kv, width, label="mode=kv (warm before moving the pin)", color="#2f6db3")
    ax.bar([x + width / 2 for x in xs], rc, width, label="mode=recompute (control)", color="#c8763c")

    for x, value in zip(xs, kv):
        ax.text(x - width / 2, value + 0.02, f"{value:.1%}", ha="center", fontsize=9)
    for x, value in zip(xs, rc):
        ax.text(x + width / 2, value + 0.02, f"{value:.0%}", ha="center", fontsize=9)

    ax.set_xticks(xs, runs)
    ax.set_ylim(0, 1.10)
    ax.set_ylabel("hit rate of the first turn after the move")
    ax.set_title("A warm migration turns the next turn into a cache hit", loc="left", fontsize=11)
    ax.legend(frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.12), ncol=2, fontsize=9)
    ax.grid(axis="y", alpha=0.25)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)

    out = ROOT / "data" / "m4_kv.png"
    fig.tight_layout()
    fig.savefig(out)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
