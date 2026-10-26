import csv, pathlib
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
ROOT = pathlib.Path(__file__).resolve().parents[1]
rows = list(csv.DictReader((ROOT/"data"/"m6_p2p_transfer.csv").open(encoding="utf-8-sig")))
labels = [r["run"] for r in rows]
cold = [float(r["cold_b_s"]) for r in rows]
tran = [float(r["transfer_b_s"]) for r in rows]
import statistics as st
fig, ax = plt.subplots(figsize=(6.4, 4.0))
x = range(len(labels)); w = 0.36
ax.bar([i - w/2 for i in x], cold, w, label="cold prefill on decode card", color="#b0724a")
ax.bar([i + w/2 for i in x], tran, w, label="KV loaded from prefill card", color="#1f5fbf")
for i,(c,t) in enumerate(zip(cold,tran)):
    ax.text(i-w/2, c+0.006, f"{c:.3f}", ha="center", fontsize=8)
    ax.text(i+w/2, t+0.006, f"{t:.3f}", ha="center", fontsize=8)
ax.axhline(st.mean(cold), color="#b0724a", ls=":", lw=1)
ax.axhline(st.mean(tran), color="#1f5fbf", ls=":", lw=1)
ax.set_xticks(list(x)); ax.set_xticklabels([f"{l}\n(1466 tok)" for l in labels])
ax.set_ylabel("time to first token (s)"); ax.set_ylim(0, 0.26)
ax.set_title(f"Cross-NPU KV transfer: {st.mean(cold)/st.mean(tran):.2f}x faster than local prefill")
ax.legend(frameon=False, fontsize=8, loc="upper right"); ax.grid(axis="y", alpha=0.25)
fig.tight_layout(); out = ROOT/"data"/"m6_p2p_transfer.png"; fig.savefig(out, dpi=150)
print("wrote", out, out.stat().st_size)
