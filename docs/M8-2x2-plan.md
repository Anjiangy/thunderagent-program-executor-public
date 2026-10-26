# M8：2×2 实验计划（放置信号 × 搬家开关）

日期：2026-10-26　状态：**harness 就绪，等机器空闲再跑**

## 为什么做这个

前面的结论散落在四种对比里（亲和 vs 轮询、我们 vs 裸 TA、搬 vs 不搬、搬移 vs 重算），每一组只动了一个旋钮，却分了好几轮跑、用过三种容量口径。这一轮把**两个真正的设计决策**放进同一个矩阵，一次跑清。

- **A 轴 = 放置信号**：`pin`（数钉住的 program 个数，现状默认）vs `bytes`（数钉住的字节预算，`EXECUTOR_PLACEMENT_CAP=0.85`）
- **B 轴 = 搬家开关**：`off` vs `on`（`EXECUTOR_BALANCE=skew`，载体固定 `recompute`，因为热点已证明它比 transfer 便宜）

四格：A0B0（现状）、A0B1（现状 + 搬家）、A1B0（字节放置）、A1B1（字节放置 + 搬家）。

## 负载：让 A 轴有意义必须先制造字节偏斜

`pin` 和 `bytes` 只有在**program 大小不均**时才会给出不同选择。所以负载必须是重尾：40 个 program，70% 是 2000 词，30% 是 6000 词（`--heavy-frac 0.7 --heavy-mult 3`）。总量 40×(0.7×2000+0.3×6000)×1.4 ≈ 140K token，单卡容量 114K ⇒ **不放置就会溢出**，这正是要测的区间。

硬约束：
- 每格 3 次重复，每次**重启 executor**（避免 program 表跨 run 残留，这是 M4 踩过的假阴性来源）；
- 每次跑前**不要重启引擎**（保留 prefix cache 的真实温度），但要在同一 pod 的同一批引擎上；
- `--skew-frac 0`：不人为强制落点，让 A 轴自己决定。

## 预注册判据（先写死，避免事后叙事）

| 问题 | 判据 |
|---|---|
| 字节放置有优势吗？ | `bytes/off` 的命中率中位数 **≥** `pin/off` + 3 pt，且 p50 不劣于 +5% |
| 搬家有优势吗？ | `bytes/on` **≥** `bytes/off` + 3 pt（在字节放置已经把负载摊平之后还能再加分） |
| 搬家是不是只在放置差时有用？ | `pin/on − pin/off` **>** `bytes/on − bytes/off`（交互项为正） |
| 多一跳的固定成本是多少？ | 四格 p50 的下界 ≈ executor 单跳开销；若 `bytes/off` 的 p50 比裸 TA 高 >20%，先修这一层 |

任何一条不成立，就照实写不成立，并把它记进 EXPERIMENTS.md 的“未成立的假设”。

## 运行方式

```bash
# 在引擎所在主机上（LOCAL=1 跳过 ssh）
LOCAL=1 bash scripts/run_2x2.sh pin   off
LOCAL=1 bash scripts/run_2x2.sh pin   on
LOCAL=1 bash scripts/run_2x2.sh bytes off
LOCAL=1 bash scripts/run_2x2.sh bytes on
python scripts/plot_2x2.py        # data/m2x2.jsonl -> data/m2x2.png
```

每格 3 rep；`run_2x2.sh` 每 rep 重启 executor、读 `healthz` 自证、把整行 JSON 追加到 `data/m2x2.jsonl`。

## 代码改动

- `patches/add_token_placement.py`：给 `ProgramTable.best_instance` 加 `token_load` 参数、加 `token_load()` 与 `_PLACEMENT_CAP()`，`assign()` 在 `EXECUTOR_PLACEMENT_CAP<1` 时用字节预算排序并排除超门实例；补丁幂等，`py_compile` 通过，16 个单测仍绿。
- `tests/m6_bench.py`：新增 `--heavy-frac` / `--heavy-mult`，前缀按 program 生成（重尾 program 更长），修掉 10-25 那次编辑留下的第 103 行语法损坏。
- `scripts/plot_2x2.py`：三面板（命中率 / p50 / 吞吐），每格画散点 + 中位线。

## 已知会让实验白跑的三个坑

1. **计数器污染**：跑前若引擎已被压满，`prefix_cache_queries_total` 会积累到 1e9 量级，命中率读数失真 → 每批跑前确认 baseline queries < 1e7。
2. **executor 重启不彻底**：`pkill -9 -f 'uvicorn executor.app'` 之后必须等 3 s，否则旧进程仍占 8100。
3. **program 表残留**：同一 program_id 第二轮会继承 `prompt_body`，导致 warm 用旧上下文（M4 的假阴性根因）→ 每 rep 重启 executor 就是为了这个。
