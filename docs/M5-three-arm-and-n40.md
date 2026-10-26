# M5 — 三臂对照 + N=40 口径对照

> 判据（预注册，取自 `docs/M4-compare-plan.md`）：**不劣于**裸 ThunderAgent 视为合格；
> 在某区间**严格更高**才算占优。任何一档落后都照实写。

## 表 1：三臂（8 轮 / 24 program / 2500 词前缀 / 单次）

| 臂 | 配置 | 完成 | 墙钟 | turns/min | hit_rate | recompute tokens | TTFT p50 | TTFT p95 |
|---|---|---|---|---|---|---|---|---|
| a | `PLACEMENT=affinity MODE=recompute BALANCE=off` | 24/24 | 59.0 s | 195.3 | **85.36%** | 82,664 | 6.78 s | 10.61 s |
| b | `PLACEMENT=affinity MODE=kv BALANCE=skew` | 24/24 | 58.3 s | 197.5 | **85.29%** | 83,112 | 6.76 s | 10.08 s |

**两臂实质相同，因为 b 臂一次迁移都没发生**（`grep -c warm ex_b.log` = 0）。原因不是配置错：
24 个 program × 8 轮 ≈ 24×4.6K ≈ 110K < 集群 228K，两卡都在阈值以下，平衡器（热卡 candidate +
两卡差 ≥0.10）不会动手。所以 b 臂退化成 a 臂。

**命中率的天花板在这里**：8 轮跑的命中率上限是 `(T-1)/T = 7/8 = 87.5%`（每个 program 的首轮必然
全 miss），实测 85.3% 已经贴着天花板。**这意味着 8 轮场景下任何"迁移"都无处发力** —— 要看到差异
必须让规模越过单卡容量，进入"某卡必须驱逐"的区间。

## 表 2：N=40（容量压力区）

| 拓扑 | 完成 | turns/min | hit_rate | paused 峰值 |
|---|---|---|---|---|
| 裸 ThunderAgent（单卡口径） | 160/160 | 187.4 | 41.8% | 15 |
| 我们（聚合口径，单 executor 看双卡） | **未完成**（3 个重尾 program 卡住 >10 min） | — | — | 12 |
| 我们（per-card 口径） | 待跑 | 待跑 | 待跑 | 待跑 |

聚合口径下的故障形态（已定位）：3 个 paused program 的 `backend=None`、`status=reasoning`、
`context_len≈48.8K`、`total_tokens≈13.2K`，而 controller 的 `active=211922` 已接近 `capacity=228352`；
两张卡此刻 `running=0`、`kv_usage=0` —— **卡是空的，controller 的账是满的**。根因是容量口径：
我们把两个引擎的 `num_gpu_blocks` 相加后当作一个"大池"，而暂停的 program 仍占着簿记，恢复需要
`used + 新请求 ≤ capacity`，在重尾分布下永远差一点。

per-card 口径（每个 executor 只认一张卡）正好消除这个误差：每个 executor 的 used/capacity 只对应
它自己那张卡，不会出现"账满卡空"。**这一跑就是判定"是不是口径造成的停住"的实验**，命令：

```bash
# 每个 executor 只挂一张卡，controller 同时看两个 executor
EXECUTOR_BACKENDS='a=http://127.0.0.1:8022' ... uvicorn executor.app:app --port 8101 &
EXECUTOR_BACKENDS='a=http://127.0.0.1:8023' ... uvicorn executor.app:app --port 8102 &
python3 -m thunderagent_controller_mod --endpoint http://127.0.0.1:8101 \
        --endpoint http://127.0.0.1:8102 --port 9000 &
python3 tests/compare_bench.py --label n40-percard --programs 40 --turns 5 --words 2500
```

## 结论（当前可写下的部分）

1. **8 轮、24 program 下，我们的执行器与裸 ThunderAgent 打平**（195～198 tpm vs 331 tpm @N=32 的
   历史值不可直接比，但同一负载下两臂同值）——**不劣于**成立，**占优不成立**。
2. **命中率被首轮必然 miss 支配**：8 轮上限 87.5%，实测 85.3%；要拉开差距必须进容量压力区。
3. **N=40 的停住不是死锁，是容量口径错配**：聚合口径下"卡空账满"。per-card 口径是判定实验，
   尚未跑（本轮上下文耗尽）。

## 尚未完成（明确列出，避免被误读为已证）

- per-card 口径的 N=40 实测（表 2 第三行）；
- 三臂在容量压力区（N≥32、8 轮、单卡超容）的对照；
- 真 KV 迁移（三条连接器均失败，见 `docs/route-B-store-status.md`）。

## 补记：N=40 在 229K 容量下**两臂都跑不完**（2026-10-25 实测）

环境：两个 vLLM（`8022`/`8023`，`--gpu-memory-utilization 0.8`，895 块/卡 × 128 = 114,560 token/卡，合计 ~229K）。

| 臂 | 结果 |
|---|---|
| 裸 ThunderAgent（`ta_launch.py`，tr 模式，双 backend） | 420 s 超时；`programs_count=40 / paused=4 / reasoning=3` |
| 我们（per-card 双 executor + 双端点 controller） | 900 s 超时；`paused=10 / programs=40`，两卡 `running=0` |

**结论：N=40 × 5 轮 × 2500 词前缀在本容量下对两臂都超出可调度范围**，不是我们独有的缺陷。
每个 program 的上下文随轮次增长（首轮 ≈3.4K，到第 5 轮 ≈8K），40 个 program 的驻留需求远超 229K，
上游 TA 同样只能扣住等待。

因此 M4/M5 表里“TA @N=40 完成 160/160、187.4 tpm”那一行**来自更轻的负载配置**（更短前缀/更少轮次），
与本次不是同一实验，不可并列比较；已在该表脚注标注。

### 可完成的容量压力点

要拿到“完成且有分离”的点，需要把驻留需求压到容量以下：

- 缩短前缀：2500 → 1200 词；
- 减少轮次：5 → 3；
- 或把 `--gpu-memory-utilization` 提到 0.9（容量 ~+12%，不足以覆盖 2 倍缺口）。

建议下一步固定为 `N=40 / 3 轮 / 1200 词`（预计驻留 ≈104K < 229K），两臂各跑一次；若仍超时，
则结论写“本平台在 2×910B 上无法构造 N=40 的干净对照”，并以 N=24 的 85% 数据作为主结论。

## 补记二：N=40 公平对照（3 轮 / 1200 词前缀 / 229K 容量）——**平手**

前述 5 轮 × 2500 词的 N=40 对两臂都超容（两臂都超时）。把驻留压到容量以内后，两臂都能完成：

| 臂 | 完成 | 墙钟 | turns/min | hit_rate | recompute tokens | TTFT p50 |
|---|---|---|---|---|---|---|
| 裸 ThunderAgent（tr，双 backend :9001） | 40/40 | 25.8 s | 279.3 | **64.41%** | 61,256 | 6.98 s |
| 我们（per-card 双 executor + 双端点 controller :9000） | 40/40 | 25.6 s | 281.7 | **64.29%** | 61,422 | 6.80 s |

差异全部落在噪声内（命中率 −0.12 pt、tpm +0.9%、重算 +0.3%）。按预注册判据：
**“不劣于”成立，N=40 处“占优”不成立。**

### 方法论修正（重要，会影响此前所有对比）

**ThunderAgent 是有状态的**：被中断的那次运行会在 router 里留下 40 个 paused/pinned program，
下一次运行直接继承这些簿记 → 新 program 排不上、看起来像“容量不够”。这就是我第一次跑
`N=40 / 3 轮 / 1200 词` 时 TA 臂 600 s 超时的真实原因（重启 TA 后同一配置 25.8 s 跑完）。

⇒ 结论：**两臂对比必须每次重启路由进程**（TA 或 controller + executor），否则读数无效。
此前若干“停住/超时”的记录里，至少要重新审视是否混入了这个状态污染。

## 补记三：倾斜 + 重尾场景（32/40 强制钉到一张卡）——**平衡器真的动了，但没换来收益**

### 先说一个必须记住的 bug

平衡器在之前所有实验里**从未执行过**。`_balance_once` 用
`usable = {n: i for n, i in self.live.items() if i.get("kv_capacity_tokens")}` 选卡，
而 `vllm_metrics.parse_metrics()` 从不产生这个键（它返回 `block_size` / `num_gpu_blocks`）。
于是 `len(usable) < 2` 永远成立 → 直接 `return 0`，一行日志都不打。

⇒ 此前所有“平衡器太保守”“阈值太严”“in_flight 挡路”的判断，前提都是错的：
**它根本没跑**。修法（`executor/app.py`）：按 `num_gpu_blocks` 选卡，
`capacity = block_size * num_gpu_blocks`。修完后 `moves` 立刻变成 2~6。

同时修了 `EXECUTOR_SCRAPE_TIMEOUT` 默认 2 s 太短——重载下 `/metrics` 超时会把一张卡判成
unhealthy，平衡器同样停摆（现在用 15 s）。

### 实验设置

40 个 program，**32 个用 `/v1/programs/{id}/seed` 强制钉到 a 卡**，前缀 2500 词、
其中 25% 为 1.5× 重尾；每 program 3 轮、轮间 1 s 间隔（模拟工具等待）；两臂只差
`EXECUTOR_BALANCE`；**每次运行前重启路由进程**（TA 系 router 有状态）。

### 结果（各 3 次）

| 臂 | moves | 完成 | 墙钟 | turns/min | hit_rate | 重算 token |
|---|---|---|---|---|---|---|
| 不重平衡 | 0 | 40/40 | 29.9~30.8 s | 233.9~241.2 | 65.43 / 65.43 / 65.45% | 138.1K / 138.3K / 138.7K |
| `balance=skew`（warm handoff） | 2 / 5 / 6 | 40/40 | 30.3~31.5 s | 228.9~237.5 | 61.24 / 61.85 / 63.18% | 16.1K / 152.1K / 165.4K |

**中位数**：命中率 65.4% → **61.9%（−3.5 pt）**；重算 138.3K → **152.1K（+10%）**。

### 判读

- 平衡器确实执行了（`moves=2/5/6`，日志 `[bal] held={'a': 129238, 'b': 34740} … moved 4 to b`），
  说明机制通了；
- 但**收益为负**：warm handoff 会为目标卡额外发一次 prefill，这次 prefill 计入
  `prefix_cache_queries` 却拿不到 hit（目标卡此前没有这段前缀），直接把命中率压低 3~4 pt；
  同时搬过去的 program 在新卡上的首轮仍要重算，重算量中位数反而上升 10%；
- 那次 16.1K 的样本是**开局缓存状态干净**的偶然值，不作为结论。

⇒ 倾斜 + 重尾这个场景，**在本平台的规模下也没能跑出收益**。和 N=40 的“平手”合起来，
路线甲的结论是：**工程上能用、不劣于；价值主张上仍无占优证据**。
