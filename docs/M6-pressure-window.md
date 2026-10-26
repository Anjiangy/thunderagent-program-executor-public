# 人为制造"搬运环境"：热卡接近满时，平衡器值多少

日期：2026-10-25　pod `<pod-name>`
引擎：a = `kv_producer`(:8110)、b = `kv_consumer`(:8111)，各 372 块 × 128 = 47,616 token
负载：40 program × 8 轮 × 15 s 空窗 × **900 词前缀（≈1561 token/program）**，`--skew-frac 0.92`

## 关键前提：这一轮热卡真的到了墙上

- bench 实测 `forced_to_a = 18`（钉卡没到预期 36，但热卡负载已 ≈ 18×1561 + 亲和自然分到的约 11 个 ≈ **45k / 47.6k = 0.95**）。
- 证据不是估算：**关掉平衡器时命中率只有 7.38%**（4 次跑：7.31 / 7.66 / 7.01 / 7.46），p50 1.46 s——这是典型的"每轮都在互相驱逐"。

## 三组结果（中位数）

| 组 | 命中率 | 重算 token | 吞吐 | p50 | 搬家 |
|---|---|---|---|---|---|
| 关平衡器（4 次） | **7.38%** | 450,780 | 119.1 tpm | 1.46 s | 0 |
| 平衡器 + KV 搬运（2 次） | 49.98% | 262,588 | 138.3 tpm | 0.49 s | 13 |
| 平衡器 + 重算交接（2 次） | **54.39%** | **230,414** | **140.6 tpm** | 0.49 s | 13 |

配置：`EXECUTOR_BALANCE=skew`、`HOT=0.80`、`COLD=0.50`、`INTERVAL=6`（提前搬，不等真超容）。

## 三条结论

1. **"把空闲 program 挪走"确实是有效杠杆**：命中率 +42.6 pt（7.38 → 49.98），重算 −42%，p50 从 1.46 s 降到 0.49 s（3×），吞吐 +16%。这是整个 M6 里第一次出现这么大的一档差。
2. **但赢的是"提前搬"这个策略，不是"KV 搬运"这个技术**：同样 13 次搬家，重算交接反而高 4.4 pt、少 12% 重算量。原因有二：(a) pull 自身要发请求，计入 queries 但不产生 hits，命中率分母被做大；(b) 热卡已经接近满时，源块可能已被驱逐，pull 只能拿到一部分。
3. **与前面的容量墙结论一致**：0.44 时搬家无意义（没触发），0.75 时微弱领先但更慢，1.11 时明显更差；**只有在 0.9~0.95、且冷卡有余量的窗口里，"提前搬"才成立**。

## 对"更高并发"的回答

并发再往上（例如 60~80 个 program 抢两台卡）不会制造这个窗口，反而会破坏它：
- 每张卡都过墙 → 没有任何一张卡有余量接收搬来的 program → 搬了也只是换一张卡继续驱逐；
- ThunderAgent 在这种区间会 park（暂停）大部分 program（我们之前 N=40 长前缀那轮 900 s 跑不完、controller paused=10 就是这情况）；
- 被 park 的 program 的 KV 早已被驱逐，**没有东西可搬**。

所以正确的做法是：**把负载压在"刚要好、还没过墙"的窄带里，用提前搬把它拉回来**，而不是把并发推到更高。

## 为什么这个窗口值得继续投入

SWE-agent 的流量天然落在窄带附近且有大空窗：
- 单实例上下文 20~30k，两卡 95k → 并发 3~4 个就接近墙，20~40 并发必然深过墙；
- 但工具调用空窗 30 s+，而一次 capture+pull 只需 0.27 s，搬家的时延代价被空窗完全吸收；
- 因此正确的设计不是"过墙后抢救"，而是**在每个 program 进入工具空窗时判断热卡是否将过 0.85，若过则把它（或别人）挪到冷卡**。

## 复现

```
EXECUTOR_BALANCE=skew EXECUTOR_BALANCE_HOT=0.80 EXECUTOR_BALANCE_COLD=0.50 \
EXECUTOR_BALANCE_INTERVAL=6 EXECUTOR_MODE=kv \
EXECUTOR_MIGRATE_CARRIER=transfer|recompute EXECUTOR_MIGRATE_FALLBACK=recompute \
  python3 -m uvicorn executor.app:app --port 8100
python3 tests/m6_bench.py --label X --arm X --programs 40 \
  --prefix-words 900 --turns 8 --skew-frac 0.92 --gap-s 15
```
数据：`data/m6_pressure_window.csv`；图：`data/m6_pressure_window.png`。
