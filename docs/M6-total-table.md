# M6 总表：三条臂、同一台 pod、同一晚（最终结论）

日期：2026-10-25　pod `<pod-name>`
引擎：a = `kv_producer`(:8110, device 0)、b = `kv_consumer`(:8111, device 1)，各 `372 块 × 128 = 47,616` token
负载：40 program × 8 轮 × 15 s 空窗；前缀 600 词（≈1050 token/program，每卡 21k / 47.6k）
裸 TA：`scripts/ta_launch.py --backends 8110,8111`（配置在 import app 之前 set，绕开 app.py 的默认 `localhost:8000` 冻结）

## 总表

| 场景 | 臂 | 命中率 | 重算 token | 吞吐 | p50 | p95 | 搬家 |
|---|---|---|---|---|---|---|---|
| 均匀（20/20） | 裸 TA | **81.59%** | **60,408** | 157.6 tpm | 0.363 s | 0.451 s | — |
| 均匀（20/20） | recompute handoff | 81.11% | 61,728 | **159.8 tpm** | **0.340 s** | **0.444 s** | 0 |
| 均匀（20/20） | transfer（KV pull） | 79.73% | 66,208 | 158.3 tpm | 0.354 s | 0.452 s | 0 |
| 倾斜 34/40（0.75） | recompute handoff | 74.11% | 85,848 | 152.4 tpm | 0.365 s | 0.567 s | 5 |
| 倾斜 34/40（0.75） | transfer（KV pull） | 75.24% | 83,490 | 151.2 tpm | 0.432 s | 0.577 s | 5 |
| 倾斜 34/40（1.11 超容） | recompute handoff | 51.16% | 246,264 | 136.9 tpm | 0.473 s | 1.466 s | 12 |
| 倾斜 34/40（1.11 超容） | transfer（KV pull） | 43.49% | 295,670 | 131.5 tpm | 0.707 s | 1.470 s | 12 |

## 结论

1. **没有任何一档我们赢过裸 TA**。唯一可比的一档（均匀）裸 TA 81.59% 高于我们两臂 1.5~1.9 pt，重算也最少。原因是它自己的放置就带 program 级容量记账，而我们多了一跳 executor，且放置只按"钉住的 program 数"排序。
2. **转移载体在 0.75 容量略优（+1.1 pt、重算 −2.7%），但 p50 慢 18%**；在超容档明显更差（−7.7 pt、重算 +20%、p50 慢 50%）。机制：搬家要付一次 capture+pull，而超容时源卡块已被驱逐，搬过去也命中不了。
3. **均匀铺卡时搬运根本不触发**（moves=0），三条臂在噪声内——这一档测的是"谁的前端更省事"，不是搬运能力。
4. 因此路线甲的定位只能是：**能跑、正确、可复现，但不占优**。要占优必须改变"谁决定放置"（把 program 级容量记账放进决策点），而不是换搬运载体。

## 与论文/他机数据不可并表

之前几张图里的裸 TA 数字来自另一台 pod（228K 集群、2500 词），与上表不同容量，不能并列。

## 复现

```
# 引擎：scripts/start_prod.sh / start_cons.sh（见 README）
# 裸 TA
PYTHONPATH=<server-dir>:<server-dir> python3 <server-dir> --backends http://127.0.0.1:8110,http://127.0.0.1:8111 --port 9001
BENCH_EXE=http://127.0.0.1:9001 BENCH_NO_CONTROL=1 python3 tests/m6_bench.py --label ta --arm ta \
  --programs 40 --prefix-words 600 --turns 8 --skew-frac 0 --gap-s 15
# 我们两臂：EXECUTOR_MIGRATE_CARRIER=transfer|recompute，--skew-frac 0 / 0.85
```
