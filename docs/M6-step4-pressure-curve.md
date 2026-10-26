# Step 4：压力—收益曲线（真正挤到 0.75 与超容两档）

日期：2026-10-25　pod `<pod-name>`
引擎：a = `kv_producer`(:8110)、b = `kv_consumer`(:8111)，各 372 块 × 128 = 47,616 token
负载：40 program × 8 轮 × 15 s 空窗；`--skew-frac 0.85` 经 `/seed` 直接钉，**`forced_to_a` 实测 34/40**（两档都是 34）
臂：`EXECUTOR_MIGRATE_CARRIER=transfer|recompute`，其余完全相同

| 档 | 每 program token | a 卡负载 | 臂 | 命中率 | 重算 token | 吞吐 | p50 | p95 | 搬家 |
|---|---|---|---|---|---|---|---|---|---|
| 600 词 | ~1050 | 35.7k / 47.6k = 0.75 | transfer | 75.24% | 83,490 | 151.2 tpm | 0.432 s | 0.577 s | 5 |
| 600 词 | ~1050 | 0.75 | recompute | 74.11% | 85,848 | 152.4 tpm | **0.365 s** | 0.567 s | 5 |
| 900 词 | ~1561 | 53k / 47.6k = 1.11（超容） | transfer | 43.49% | 295,670 | 131.5 tpm | 0.707 s | 1.470 s | 12 |
| 900 词 | ~1561 | 1.11（超容） | recompute | **51.16%** | 246,264 | 136.9 tpm | 0.473 s | 1.466 s | 12 |

## 两条结论（这次站得住）

1. **0.75 容量：transfer 略优但不显著**。命中率 +1.1 pt（75.2 vs 74.1）、重算 −2.7%，但 p50 反而慢 18%（0.432 vs 0.365）。解释：搬家 5 次，每次要付一次 capture+pull（约 0.27 s）在被搬 program 的关键路径上；收益是省下一次 prefill（约 0.1 s），一进一出接近抵平，稍偏负。
2. **超容（1.11）：transfer 明显更差**。命中率 −7.7 pt（43.5 vs 51.2）、重算 +20%、p50 慢 50%。解释：超容时源卡的块本来就频繁被驱逐，`capture` 抓到的握手包可能指向已被回收的块，搬到目标卡也命中不了几次，而每次搬家仍要付全额代价（12 次搬家 × 0.27 s 的窗口）。**这正是"搬运救不了拥挤"的量化版**。

## 与裸 ThunderAgent 的关系（不能直接并表）

裸 TA 的 187.4 tpm / 41.8%（N=40、2500 词、228K 集群）是**另一台 pod、另一套容量**上跑的，不能与上表并列。可比的是相对方向：超容时我们的 recompute 臂（51.2%）明显高于裸 TA 那档（41.8%）——但那也主要来自"每卡 47.6k vs 228K"的容量差异，不是机制差异，不应拿来做结论。

## 方法学修正

- 本轮真正的修复是 `/v1/programs/{pid}/seed`：直接改 pin、不走迁移，于是 `forced_to_a` 从 0/17 变成稳定的 34/40。之前两轮"倾斜没生效"的数据只能当基线看。
- `EXECUTOR_MIGRATE_FALLBACK=recompute` 保留：transfer 失败时回退而不是 409，这样两臂的样本量一致（都 280/280）。
- 判据仍是 p50/p95 与命中率；重算 token 对 transfer 不完全公平（pull 自身消耗 query）。

## 复现

```
EXECUTOR_BALANCE=skew EXECUTOR_BALANCE_INTERVAL=10 \
EXECUTOR_BALANCE_HOT=0.75 EXECUTOR_BALANCE_COLD=0.55 \
EXECUTOR_MIGRATE_CARRIER=transfer|recompute python3 -m uvicorn executor.app:app --port 8100
python3 tests/m6_bench.py --label s8t600 --arm transfer --programs 40 \
  --prefix-words 600 --turns 8 --skew-frac 0.85 --gap-s 15
```
