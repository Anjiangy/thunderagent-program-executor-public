# M4 对比：本方案（controller → executor → 双卡 vLLM）vs 裸 ThunderAgent

日期：2026-10-23　　机器：Ascend 910B2 ×2（device0=8022 / device1=8023）
模型：Qwen2.5-14B-Instruct，max-model-len 32768，`--enable-prefix-caching`，每卡 892 blocks × 128 = 114176 token，集群 228352 token。

## 1. 两臂是什么

| 臂 | 拓扑 | 节流/放置 |
| --- | --- | --- |
| `ta` | client → ThunderAgent `tr` 模式（单进程，直接管 8022/8023） | 上游官方 MultiBackendRouter：per-backend 容量记账 + BFD 恢复 |
| `ours` | client → controller :9000（同一个上游 router 的薄适配器）→ executor :8100 → 8022/8023 | 同一个 router，容量口径换成集群 `Σused/Σtotal`；program 落点由 executor 的 `best_instance()`（钉住数→running→kv_usage）决定 |

两臂走同一份负载生成器（`tests/compare_bench.py`）：每 program 独立前缀（`--size-skew 0.2` 时 20% 的 program 前缀 ×3，制造重尾），4 轮，`--words 2500`（约 4.6k token/program）。

## 2. 能跑的档位：持平

| N | 臂 | 完成 | turns/min | 命中率 | p50 | 暂停峰值 |
| --- | --- | --- | --- | --- | --- | --- |
| 24 | ta | 96/96 | 309 ~ 318 | 0.734 | 3.05 ~ 3.45 s | 0 |
| 24 | ours | 96/96 | 307.5 | 0.733 | 3.04 s | 0 |
| 32 | ta | 128/128 | 331.6 | 0.734 | 3.07 s | 0 |
| 32 | ours | 128/128 | 331.1 | 0.735 | 3.07 s | 0 |

差异在噪声内（同一台机器、同一模型、同一份 prompt）。这一档没有提升，符合 `docs/M4-compare-plan.md` 里写好的预注册预期（多一跳 executor 会吃掉一点吞吐）。

## 3. 没法比的档位：N>=36 撞容量墙

- ta @ N=40（160 请求）：51 s 完成，187 turns/min，命中率 0.418，p50 11.16 s，暂停峰值 15。即 40×约 6.2k 约 248k > 228k，上游自己进入"暂停—恢复"轮转，吞吐砍半、命中率腰斩。
- ours @ N=40：10 分钟未完成。现场：executor `/healthz` 中 `active_program_tokens≈212k`（容量 228352），controller 有 3 个 program 处于 `paused` 且 `status=reasoning`（手里有未完成的请求）、`backend=None`；同时两张卡 vLLM 都是 `running=0 / kv_usage=0`——卡是空的，请求却停在 controller 里。
- ta @ N=36：同样停住（`paused 2`，>8 分钟）。

所以"停在少量重尾 program"不是我们独有：上游原生行为在总负载贴住容量时都会出现。区别在于 N=40 那次 ta 恰巧先跑完了，ours 撞上了三个都在等待的重尾 program。

### 机制解释（本次对比的真正结论）

上游的暂停/恢复模型假设每个 backend 的 KV 就是那个 backend 的容量：`_greedy_resume` 把 program 放进"还有剩余容量"的 backend，然后指望它立刻在那边跑起来。

我们的 executor 把两张卡聚合成一个 backend（容量 228k）。于是出现一个上游模型里不存在的状态：

1. controller 认为"集群还有 16k 空闲"，于是恢复一个 13.2k 的 program；
2. executor 按亲和把它钉在它原来那张卡上，而那张卡可能已经满了（`kv_usage` 高）；
3. 这个 program 的 prefill 在那张卡上排队，controller 这边却已经把它算成 ACTIVE 占用，于是再没有容量去恢复别人；
4. 结果：卡空着、controller 满着、请求挂着。

这正是容量模型清单（07 文档 P2 条）标出的风险：聚合口径让 controller 的"有容量"与执行侧的真实可放置性脱钩。在 N<=32（总负载 < 一卡容量）时两者一致，所以持平；一旦跨过一卡容量，聚合就变成了"看起来有空间、实际放不下"。

## 4. 结论

1. 叠加（亲和 + 预热式迁移）在真机上能跑通：M2 的身份穿透、M4 的迁移命中（96.8/97.0/97.3%）都不是模拟。
2. 端到端对比没有赢：N=24/32 与裸 ThunderAgent 持平（差 0.2%~3.5%），N>=36 两臂都进入容量墙，本方案还会因为"聚合 backend"多出一种上游没有的停住形态。
3. 不是实现 bug，是模型错配：要让"迁移 + 亲和"真正换成吞吐，需要让 controller 看到每张卡的容量（两个 backend），由 controller 做 program 级放置决策，executor 退化成"按 program 钉住的卡直连"。这样恢复决策与真实可放置性同构，N>32 才有意义。

## 5. 复现

```bash
cd <server-dir>
bash scripts/run_compare.sh 24 4 2500 skew
bash scripts/run_compare.sh 32 4 2500 skew
python3 scripts/summarize_compare.py <server-dir>
```

`scripts/run_compare.sh` 每次先 pkill 旧进程、用 `/health` 的 `programs_count==0` 确认起点干净；脏起点会被拒绝。

## 6. 顺带修掉的上游 bug（副产物）

`python -m ThunderAgent --backends 8022,8023 --router tr` 根本不生效：包 `__init__.py` 先 `from .app import ...`，而 `app.py` 在模块级执行 `router = _create_router()`（用默认 Config，单 backend `http://localhost:8000`），命令行参数是之后才 `set_config` 的，所以永远只有一个本地 backend。也就是说上游自带的"直连两卡"跑法一直是坏的。

因此我们写了 `scripts/ta_launch.py`：先把一个桩包塞进 `sys.modules`，让 `app.py` 的模块级创建用一个空 Config 跑过去，再 `set_config(真 Config)`，最后正常导入 `app`。本对比里 ta 臂就是用这个启动器跑的（43 个 program 时 `--backends` 才生效、`paused` 才会出现）。

> 因此，任何用 `python -m ThunderAgent --backends ...` 得出的多卡数字都应当怀疑。
