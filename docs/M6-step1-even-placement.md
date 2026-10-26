# Step 1：均匀铺卡 —— 结论与一个容量墙

日期：2026-10-25　pod `<pod-name>`
双臂：`EXECUTOR_MIGRATE_CARRIER=recompute|transfer`，40 program，8 轮，`--skew-frac 0`（不强制倾斜）

## 结果

| 前缀 | 每 program token | 每卡负载 | 单卡容量 | 命中率（两臂） | 吞吐 | p50 | 搬家 |
|---|---|---|---|---|---|---|---|
| 600 词 | ~1050 | 21k | 47.6k | 80.35% / 80.17% | 158.6 / 158.5 tpm | 0.354 / 0.353 s | 0 |
| 900 词 | ~1561 | 31k | 47.6k | 40.88/41.21% / 41.00% | 140.2 / 140.4 tpm | 0.448 / 0.449 s | 0 |
| 1800 词 | ~3092 | 62k | 47.6k | 0.00% / 0.00% | 111.4 / 111.3 tpm | 1.509 / 1.528 s | 0 |

（单卡容量 = `num_gpu_blocks 372 × block_size 128 = 47,616` token，来自 `vllm:cache_config_info`。）

## 两条结论

1. **均匀铺卡时，搬运载体没有意义**：所有配置 `balance_moves = 0`、`migrations = 0`，两臂数字在小数点后第二位重合。载体只决定“搬家怎么搬”，而这里没有家要搬。
2. **决定命中率的是容量墙，不是载体**：1050 tok/program → 80.4%，1561 → 41%，3092 → 0%。二阶导在“每卡负载 ÷ 单卡容量”接近 0.6~0.7 处最陡。之前那一轮 0%（1800 词）不是测量 bug，而是 62k 负载压 47.6k 容量的必然结果——**我先前把它当异常挂在报告里，是判断错了，这里更正**。

## 对路线甲的判断

- “把工作换个地方”只在**单卡装不下、但集群装得下**的形态里有用；均匀铺卡时它没有触发条件。
- 真正让命中率上去的是**让每张卡的派驻量落在容量内**（放置/准入），这与 ThunderAgent 的 throttling 是同一个思路，不靠搬运实现。

## 复现

```
# 服务端切臂
EXECUTOR_MIGRATE_CARRIER=transfer|recompute
python tests/m6_bench.py --label X --arm transfer --prefix-words 600|900|1800 --skew-frac 0.0
python scripts/plot_m6_even.py     # data/m6_even_placement.csv -> .png
```
