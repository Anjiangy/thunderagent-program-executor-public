"""Merge compare_bench JSON files into one markdown table."""

from __future__ import annotations

import json
import pathlib
import sys


def main() -> int:
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "compare")
    rows = []
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if "label" not in data:
            continue
        rows.append(data)

    rows.sort(key=lambda d: (d.get("programs") or 0, d.get("label") or ""))
    print("| N | arm | turns/min | hit rate | p50 s | p95 s | failed | paused peak | wall s |")
    print("|---|---|---|---|---|---|---|---|---|")
    for d in rows:
        print("| {p} | {a} | {t} | {h} | {p50} | {p95} | {f} | {pp} | {w} |".format(
            p=d.get("programs"), a=(d.get("label") or "").split("-")[0],
            t=d.get("turns_per_minute"),
            h=("%.1f%%" % (d["hit_rate"] * 100)) if d.get("hit_rate") is not None else "—",
            p50=d.get("latency_p50_s"), p95=d.get("latency_p95_s"),
            f=d.get("failed"), pp=d.get("paused_peak_sampled"),
            w=d.get("wall_seconds")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
