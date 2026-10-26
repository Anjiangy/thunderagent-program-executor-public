# Step 3：真搬运 vs 重放式交接（两组配置，各 280 请求全成功）

日期：2026-10-25　pod `<pod-name>`
引擎：a = `kv_producer`(:8110, card0)、b = `kv_consumer`(:8111, card1)，各 372 块 × 128 = 47.6k token
负载：40 program × 600 词前缀 × 8 轮 × 15 s 空窗；`EXECUTOR_MODE=kv`，唯一变量 `EXECUTOR_MIGRATE_CARRIER`

| 配置 | 臂 | 命中率 | 重算 token | 吞吐 | p50 | p95 | 搬家 |
|---|---|---|---|---|---|---|---|
| interval 30 s, 预搬 17 | transfer | 74.77% | 83,964 | 155.8 tpm | **0.389 s** | 0.576 s | 3 |
| interval 30 s, 预搬 17 | recompute | 74.97% | 82,618 | 155.0 tpm | 0.439 s | 0.588 s | 3 |
| interval 10 s, 预搬 0 | transfer | 75.32% | 83,414 | 116.7 tpm | **0.364 s** | 0.570 s | 5 |
| interval 10 s, 预搬 0 | recompute | 75.57% | 81,235 | 115.8 tpm | 0.423 s | 0.595 s | 5 |

## 结论（两组同向，可复述）

1. **命中率打平**：74.8/75.0 与 75.3/75.6，差异在噪声内。这个压力点（每卡 ~31k / 47.6k ≈ 0.65）不足以让搬运改变驻留格局。
2. **延迟有稳定收益**：p50 `0.389→0.439`、`0.364→0.423`，transfer 臂快 11%~14%；p95 也小幅更优（0.576/0.570 vs 0.588/0.595）。机制是明确的——搬家把一次 prefill 从关键路径上拿掉，换到空窗里做。
3. **重算 token 反而多 1.6%~2.7%**：warm/pull 自身会产生一次前缀消耗（记进 queries），所以"重算量"这个口径对 transfer 不公平；判据应看 p50/p95。

## 这轮修掉的三个真 bug

1. `migrate` 在 `EXECUTOR_MIGRATE_FALLBACK=none` 且 pull 失败时返回 409 却**没有清 `migrating` 标记**，导致该 program 之后所有请求永久 503（上一轮 8218 行 503 的真因）。修法：返回前先 `finish_migration(ok=False)`。
2. bench 的 `failures` 从不累加（第一轮跑出 `requests 0 / failures 0` 的假象），已改为统计 `None` 结果。
3. bench 的模型名写死为模型路径，而引擎用 `--served-model-name`，导致 320 个 404。已改为 `BENCH_MODEL` 环境变量、默认 served name。

## 已知偏差与未测

- `forced_to_a` 两次都没达到预期（17 与 0），所以"70% 挤一张卡"的强度没有真正建立；这轮结论只覆盖"每卡 0.65 左右"的温和区间。
- 更大前缀（900 词，每卡 31k）与更高压力（1800 词，62k 超容）没有在本轮重跑；超容那组此前已测出命中率 0%（容量墙），搬运无法补救。
- `usage.prompt_tokens_details.cached_tokens` 在 vLLM 0.23 升腾上恒为 None，无法做逐请求归因，只能用集群计数器差分。

## 复现

```
# 服务端起臂
EXECUTOR_MIGRATE_CARRIER=transfer|recompute \
EXECUTOR_MIGRATE_FALLBACK=recompute \
EXECUTOR_BALANCE=skew EXECUTOR_BALANCE_HOT=0.75 EXECUTOR_BALANCE_COLD=0.55 \
EXECUTOR_BALANCE_INTERVAL=10 \
python3 -m uvicorn executor.app:app --host 127.0.0.1 --port 8100
# 客户端
python3 tests/m6_bench.py --label s6t --arm transfer --programs 40 \
  --prefix-words 600 --turns 8 --skew-frac 0.85 --gap-s 15
```
