# M6 — 真 KV 迁移的专场景负载 + SWE-agent 接入（设计稿）

> 状态：设计完成，**未执行**。原因：2026-10-25 16:01 起升腾入口 `<ascend-host>:<ssh-port>` 不可达
> （`Test-NetConnection` 报 closed，`nc`/ssh 均 Connection refused → 后转 timeout），pod 疑似被重建/回收。
> 恢复后按本文顺序执行即可。

## 1. 目标与判据

**要证明的命题**：当「常驻集 > 单卡容量」且「program 会在工具等待期空出」时，把 KV 搬到另一张卡
（真迁移）比在目标卡重算前缀更划算。

**预注册判据**（先写死，避免事后挑选）：

| 判据 | 含义 |
|---|---|
| P1 | 迁移臂的 `recompute_tokens` 比 recompute 臂低 ≥ 25% |
| P2 | 迁移臂的 `ttft_p95` 不高于 recompute 臂 |
| P3 | 迁移的搬运耗时 ≤ 该 program 前缀在目标卡重算耗时的 1/3 |
| P4 | 全程无 `KV load failure`，无 5xx |

四条全过 = 成立；P1 或 P3 不过 = 该通道在当前前缀规模下不划算（照实报）。

## 2. 为什么现有负载测不出来

`three_arm_bench.py` 的 24 program × 2500 词 ≈ 110K token，**装得进一张卡**（114K），所以
平衡器不触发、迁移一次都没发生（b 臂 `warm=0`）。必须让负载**结构性超容**：

- 单卡容量：892 blocks × 128 = 114,176 token；
- 目标：常驻集 ≈ 1.4~1.6 × 单卡容量，即每卡平均 ~1.2×，才会持续驱逐。

## 3. 负载形状（专场景）

```
program i:  [长前缀 P_i ~4600 tok] → turn1 → [工具等待 w_i] → turn2(追加) → [等待] → turn3 ...
             ↑ 前缀互不相同（避免跨 program 共享缓存）
```

三个必要性质：

1. **前缀互不同**：`make_prefix(rng, words)` 每 program 独立，禁止共享开头（否则跨 program 命中，测的是共享而不是迁移）。
2. **工具等待窗口足够长**（`--gap-s 15~30`）：迁移必须发生在 program 不在算的时段，否则搬 KV 会挡住关键路径。
3. **落点倾斜可控**（`--skew`）：把 70% program 的首轮钉到 a 卡，制造真实的容量失衡；这是迁移唯一有价值的场景。

参数矩阵（首轮）：

| 参数 | 值 | 理由 |
|---|---|---|
| program 数 | 32 / 40 | 40×4.6K≈184K ≈ 1.6× 单卡容量 |
| 轮数 | 8 | 命中率天花板 (T-1)/T=87.5%，留出迁移可改善的空间 |
| 前缀 | 2500 词 ≈ 4600 token | 与既有实验可比 |
| 工具等待 | 15 s | 足够搬 460MB KV（按 20GB/s 约 25ms，留出握手余量） |
| 倾斜 | 70% 首轮钉 a | 制造“a 超容、b 空闲” |

## 4. 迁移触发（本仓库现成接口）

- `POST /v1/programs/{pid}/seed`（直接写 pin）；
- `POST /v1/programs/{pid}/migrate`，`{"target":"b","mode":"kv"}`（`mode=kv` 会先在目标卡预热再挪 pin）；
- 触发条件（M6 起改为显式驱动）：`in_flight == 0`（program 正在等工具）且 `held[a] - held[b] > 0.5 × 单卡容量`。

> 注意：现有的 `balance=skew` 是**自动**触发，在倾斜+重尾实验里出现过负收益（命中 65.4→61.9）。
> M6 要把它改成**在工具等待窗口内触发**，这是与既有实验的关键区别。

## 5. 度量

每次跑都同时记：

- `recompute_tokens = Δqueries − Δhits`（vLLM `/metrics` 差分，指标名必须带 `_total`）；
- `hit_rate = Δhits / Δqueries`；
- 迁移次数、每次搬运耗时（executor 日志 `migrate ok ... warmed=... ms`）；
- `ttft_p50/p95`（客户端侧）、`failures`、`paused_peak`（controller `/health` 轮询）。

## 6. SWE-agent 接入

复用 Fig5 复现那套（`<bench-home>/sweep_fig5_msa.py` + mini-swe-agent + 93 实例 filter），只改两点：

1. **program_id = job_id**（已支持），让每个实例天然成为独立 program；
2. **工具执行即等待窗口**：mini-swe-agent 每步跑测试约 30 s，是天然的“可在其间迁移”的窗口 → 比合成 `--gap-s` 更真实。

对比臂：`EXECUTOR_BALANCE=skew`（自动）vs `off`；两边都过 controller，并发 24/32，步数 30。

## 7. 恢复后的执行顺序

1. 确认 pod 与两张 NPU 空闲：`npu-smi info`、`ps -ef | grep vllm`；
2. 起 vLLM 8022/8023（`<root>/start_vllm.sh`，`--enable-prefix-caching`）；
3. 起 executor（`EXECUTOR_BALANCE=off` 与 `skew` 两臂各一次）；
4. 先跑 `three_arm_bench.py --programs 40 --turns 8`（基线，无倾斜）；
5. 再跑 `bench_skew.py --skew 0.7 --gap-s 15`（专场景），两臂各 3 次；
6. 最后接 SWE-agent。

## 8. 与真迁移通道的关系

本文件只定义“怎么测”。通道本身仍是未闭合项：

- store 通道：元数据（lookup）已通，数据面 `adxl engine connect` 超时未解；
- p2p 通道（`MooncakeConnectorV1`）：双引擎已能带 `kv_connector_extra_config{prefill,decode}` 就绪，
  但 `do_remote_prefill` 需要 `remote_bootstrap_addr` + `remote_engine_id`，须由官方
  `mooncake_connector_proxy.py` 提供（本 pod 上 8998 未监听，需 proxy 自己拉起 bootstrap）。

通道未通之前，本节实验只能跑 recompute 臂作为基线。
