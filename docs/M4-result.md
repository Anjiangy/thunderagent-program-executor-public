# M4 — `mode=kv`：重算式交接（recompute handoff）

> **术语说明（2026-10-25）**：本文里的 `mode=kv` 动作是 **recompute handoff（重算式交接）**，
> 不是 KV migration：它不搬运任何 KV block，只是在 program 停顿（调工具）期间把该 program 的
> prompt 在目标卡上重新 prefill 一遍，再把 pin 指过去。收益是"把这次 prefill 挪出关键路径"
> 与"重新平衡驻留"，代价是多算一次前缀；跨卡没有任何数据流动。
> 真迁移（block copy）在本平台不可用，三条连接器的证据见 `docs/route-B-store-status.md`。

## 问题

M3 结束时，`mode=kv` 在 executor 里直接返回 501。理由很硬：M0 已经确认这条栈上没有
KV block 搬运通道（`vllm_ascend` 导入即崩、NPU 上没有可用的 KV connector）。所以
"迁移" 如果要有意义，只能是**另一种实现**：

> 把 program 自己的 prompt 重放给目标实例，让 vLLM 做一次预填充，把 block 留在它的
> prefix cache 里，然后再把 pin 挪过去。

代价是一次 prefill；收益是这次 prefill 可以发生在 program 停顿（等工具返回、被
controller 挂起）的时候，不在关键路径上。这也是路线甲和路线乙唯一的分界线：乙要搬
block，甲只负责"目标上先有 block"。

## 实现

`executor/programs.py`
- `ProgramEntry` 记 `prompt_path / prompt_body / prompt_tokens / warmed_tokens`。
- `record_prompt()`：**保留最近一次请求体**（见下面的 bug）。
- `record_warmed()`：记预热成功的 token 数。
- `migration_candidates(instance)`：同实例、非 migrating、无 in-flight、有 prompt 的
  program，按 prompt 大小降序——大的先搬，收益最大。

`executor/app.py`
- `_warm_target()`：把记录的请求体复制一份，`stream=False`、`max_tokens=1`、
  `temperature=0`，POST 到目标实例的同一路径。返回 `warm_ms` 与 `warmed_prompt_tokens`。
- `_do_migrate()`：`begin_migration` → 等 in-flight 归零（超时 409 回滚）→ `mode=kv`
  时预热，预热失败且非 `allow_degraded` 则 409 且 pin 不动 → `finish_migration`。
- `_balance_once()`：取 kv 使用率最高/最低的两个实例，hot ≥ 0.90 且 cold ≤ 0.65 时搬
  该实例上最大的可搬 program，每个周期最多一次，`EXECUTOR_BALANCE=skew` 打开。

## 探针

`tests/m4_kv_probe.py` 两臂同构：

1. phase 0：每个 program 先用一个**极短 dummy prompt** 建立 pin（若落点不对就
   `mode=recompute` 挪回 source）。这一步是为了让**真实长前缀只出现在 source 上**；
   否则控制臂可能命中自己之前留在目标上的缓存，测出来的"命中"全是污染。
2. phase 1：在 source 上跑 2 轮，逐轮追加工具输出。
3. phase 2：两臂都迁到从未见过该前缀的 target，一个 `mode=kv`（先预热），一个
   `mode=recompute`（不预热）；迁完各跑一轮，读 target 自己的
   `vllm:prefix_cache_queries_total / hits_total` 差分。

判据：`kv > 0.5` 且 `recompute < 0.2`。

## 结果

| run | 臂 | 迁移后首轮 queries | hits | 命中率 |
|---|---|---|---|---|
| m4c | mode=kv | 3966 | 3840 | **96.8%** |
| m4c | recompute | 3961 | 0 | 0.0% |
| m4f | mode=kv | — | — | **97.0%** |
| m4f | recompute | 3942 | 0 | 0.0% |
| m4g | mode=kv | — | — | **97.3%** |
| m4g | recompute | 3978 | 0 | 0.0% |

![M4 probe](data/m4_kv.png)

三次独立重跑都在 96.8%~97.3%，控制臂稳定在 0。也就是说：**在这条栈上，"迁移"只能是
重放式再物化，而它确实能把 program 的下一次调用变成目标实例上的缓存命中。**

## 一个真 bug（值得记）

`record_prompt()` 最初的设计是"保留最大的那个 prompt"，理由是"最大的最接近 program
的当前上下文"。实测拿到过一次假阴性：warm 报成功，迁移后首轮却 0 命中。

原因：program 表比一次实验活得更久，同一个 `program_id` 在不同 run 里被复用，表里还
留着上一次 run 的**更长**请求体；新请求体更小，于是被丢弃，warm 重放的是上一次的
上下文——目标实例上确实有缓存，但和这次要发的 prompt 不是同一个前缀。

改法：**最新写入胜出**。下一次调用是在这次上下文上追加，所以"最近一次请求体"才是
program 真正会发出去的那个 block 前缀。探针也改成每个 run 用独立 `program_id`。

## 边界

- 只验证了"迁移后首轮命中"，没有验证**节流区间里迁移的收益**（balance loop 已实现，
  但还没在饱和负载下跟"只节流"对比——服务器在准备这组实验时失联了）。
- 预热发生在 pin 挪动之前，此时 program 的旧 pin 仍指向 source；如果这期间它有请求
  进来，会打到 source 上（迁移窗口对同一个 program 返回 503 + `Retry-After`）。
- 预热是一次真实 prefill，吃目标实例的算力；这组实验里 warm 约 0.5–0.6 s。
- 绝对数字不可与论文直接比：模型（Qwen2.5-14B）、卡（2×910B2）、规模都不同。

## 复现

```bash
# 目标机：两个 vLLM 实例 + executor
EXECUTOR_BACKENDS='a=http://127.0.0.1:8022,b=http://127.0.0.1:8023' \
EXECUTOR_PLACEMENT=affinity EXECUTOR_BALANCE=off \
python3 -m uvicorn executor.app:app --host 127.0.0.1 --port 8100

python3 tests/m4_kv_probe.py --label m4f --words 2600
```
