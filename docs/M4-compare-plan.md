# M4-B：和 ThunderAgent 本体对比（预注册设计）

## 对比的是什么

两条臂跑的是**同一份 controller 代码**（ThunderAgent，钉 `7ddc861`）、同两个 vLLM 实例、
同一批 program 与 prompt：

| 臂 | 链路 | 多了什么 |
|---|---|---|
| `ta` | client → ThunderAgent(:9001) → vLLM 8022/8023 |  ничего，就是上游原样 |
| `ours` | client → ThunderAgent(:9000) → executor(:8100) → vLLM 8022/8023 | 集群口径容量 + program 亲和 + 预热式迁移 |

所以差量**只**来自我们这一层，不是换了个调度器。

## 预期从哪里来（机制，不是口号）

上游的容量账是**按单卡**算的：一个 program 一旦落在某张卡上就钉在那儿，卡满了它只会把
自己的 program 挂起/放行，**不会把 program 挪到另一张卡**。于是有两种浪费：

1. 两张卡负载不平时，空的那张只能干看着（`has_capacity` 是按卡判断的）；
2. program 的上下文单调变长，钉住的卡会先满，先满就暂停，而另一张卡还有余量。

我们的 executor 把两张卡当成**一个引擎**报容量（`num_gpu_blocks=Σ`、`usage=Σused/Σtotal`），
并在 `hot≥0.90 且 cold≤0.65` 时把热卡上**最大的可搬 program** 预热到冷卡再挪 pin。

因此预期是：

- **负载对称、总量未超单卡容量**（N≤16）→ 两臂应当基本持平（此时没有任何搬迁要发生，
  我们还多一跳 HTTP，甚至可能略慢）；
- **总量超过单卡但未超集群**，且 program 大小有重尾（`--size-skew`，20% 的 program 前缀 ×3）
  → 我们应当赢，赢在"不把缓存钉死在满卡上"；
- **总量超过集群容量**（N≥48 全部长前缀）→ 两臂都会进入节流/重算互拖，这一档只报数，
  不作为结论。

## 判据（先写死，免得事后解释）

对每个 N 报：`turns/min`、`hit rate`、`p50/p95`、`failed`、`paused peak`、`wall`。

- **达标**：`ours` 在每个 N 上 `turns/min` 与 `hit rate` 都不低于 `ta`，且至少在中高负载
  档（N=32/40）严格更高；
- **不达标**：出现任一 N 上 `ta` 更高 → 照实报告，并把该档的机制解释写清楚
  （例如低负载时多一跳的代价、或搬迁本身的开销吃掉收益）。

我不打算用"挑一个好看的 N"来交差：数值先跑、先落盘（`compare/*.json`），再写结论。

## 怎么跑（服务器回来后）

```bash
scp -P <ssh-port> -r tests scripts root@<ascend-host>:<server-dir>
ssh -p <ssh-port> root@<ascend-host>
cd <server-dir>
bash scripts/run_compare.sh "16 32 40" 4 2500 skew
python3 scripts/summarize_compare.py <server-dir>
```

`run_compare.sh` 自己负责起停两条臂（含杀掉残留进程）、每个 (arm, N) 落一个 JSON。

## 已知会让"我们"吃亏的地方（先说清楚）

1. **多一跳**：`client → controller → executor → vLLM`，低负载时这纯粹是开销；
2. **预热要吃算力**：搬迁前的那次 `max_tokens=1` prefill 是真的算力消耗；
3. **平衡器阈值**：`hot≥0.90` 才动手，均匀负载下可能整场都不触发——那不是 bug，
   是"没有不均衡可修"。
