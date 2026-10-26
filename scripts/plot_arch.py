"""Render the route-A architecture as a PNG (matplotlib, offline)."""
from __future__ import annotations
import pathlib
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib import font_manager

for name in ("Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "Source Han Sans SC"):
    if any(f.name == name for f in font_manager.fontManager.ttflist):
        plt.rcParams["font.family"] = name
        break
plt.rcParams["axes.unicode_minus"] = False

ROOT = pathlib.Path(__file__).resolve().parents[1]
COL = {"client": "#8b93a1", "ctl": "#e07b39", "exe": "#1f5fbf", "be": "#3f8f4f", "note": "#6b7280"}

fig, ax = plt.subplots(figsize=(13.6, 6.7))
ax.set_xlim(0, 100); ax.set_ylim(12, 100); ax.axis("off")


def box(x, y, w, h, title, lines, color, fs=11, lw=1.8):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h,
                                boxstyle="round,pad=0.6,rounding_size=1.2",
                                linewidth=lw, edgecolor=color, facecolor=color + "14"))
    ax.text(x, y + h / 2 - 3.2, title, ha="center", va="top", fontsize=fs, color=color, weight="bold")
    ax.text(x, y + h / 2 - 8.0, "\n".join(lines), ha="center", va="top", fontsize=fs - 2.4, color="#333")


def arrow(p1, p2, color="#555", style="-|>", ls="-", lw=1.8, rad=0.0):
    ax.add_patch(FancyArrowPatch(p1, p2, arrowstyle=style, mutation_scale=16,
                                 linewidth=lw, color=color, linestyle=ls,
                                 connectionstyle=f"arc3,rad={rad}"))


def label(x, y, text, color="#555", fs=8.6, style="italic"):
    ax.text(x, y, text, ha="center", va="center", fontsize=fs, color=color, style=style,
            bbox=dict(boxstyle="round,pad=0.28", fc="white", ec="none", alpha=0.92))


# ---- nodes -----------------------------------------------------------------
box(12, 78, 21, 20, "客户端 / agent 框架",
    ["mini-swe-agent", "program_id = 一个任务", "多轮：LLM → 工具 → LLM"], COL["client"])
box(39, 78, 22, 20, "controller :9000",
    ["ThunderAgent 薄适配器", "准入 / 暂停 / 恢复", "每后端容量记账"], COL["ctl"])
box(66, 78, 23, 20, "executor :8100（本工作）",
    ["program 表 → 卡 亲和", "放置：pin / bytes / fill", "搬家：hot·cold·gap"], COL["exe"])
box(88, 62, 19, 15, "vLLM A :8110",
    ["kv_producer", "前缀缓存"], COL["be"])
box(88, 32, 19, 15, "vLLM B :8111",
    ["kv_consumer", "前缀缓存"], COL["be"])
box(56, 34, 24, 12, "/metrics 聚合",
    ["Σ blocks × block_size → 容量"], COL["note"], fs=10)

# ---- edges -----------------------------------------------------------------
arrow((22.6, 78), (27.9, 78)); label(25.2, 81.4, "HTTP")
arrow((50.1, 78), (54.4, 78)); label(52.2, 81.4, "chat/completions\n+ program_id_hint")
arrow((77.6, 78), (83.0, 64.5), rad=-0.1); label(81.6, 72.6, "首次落点 / 已钉住")
arrow((77.6, 76), (83.0, 33.0), rad=0.22); label(75.8, 46.0, "溢出 / 搬家后")
arrow((88, 54.5), (88, 39.5), color=COL["be"], ls="--")
label(93.2, 46.0, "capture + pull\n(MooncakeConnectorV1 p2p)", color=COL["be"], fs=8.2)
arrow((68.0, 40.0), (70.5, 74.0), color=COL["note"], ls=":", rad=0.18)
label(63.6, 52.0, "每 10 s 刷新\n占用与容量", color=COL["note"], fs=8.2)

# ---- axes / notes ----------------------------------------------------------
ax.text(3, 96, "路线甲：控制面管准入，executor 管放置与搬家，后端只管块", fontsize=13.5, weight="bold", color="#111")
ax.text(3, 60,
        "决策归属：\n"
        "  · 谁可以跑 —— controller（并存 program 的上下文总量 ≤ 集群容量）\n"
        "  · 跑在哪张卡 —— executor（program → instance 的钉住关系）\n"
        "  · 块在不在 —— vLLM（前缀缓存的命中与驱逐）",
        fontsize=9.2, va="top", color="#333", linespacing=1.7)
ax.text(3, 22,
        "搬家的四个同时条件：hot ≥ 0.75 · cold ≤ 0.55 · gap ≥ 0.10 · 候选 in_flight = 0\n"
        "载体可替换：recompute（目标卡重放一次 prefill）或 capture+pull（从源卡取块）",
        fontsize=9.2, va="top", color="#333", linespacing=1.7)

fig.tight_layout()
out = ROOT / "data" / "m9_architecture.png"
fig.savefig(out, dpi=160, facecolor="white")
print("wrote", out, out.stat().st_size)
