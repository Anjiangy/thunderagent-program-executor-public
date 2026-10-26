# M8-B：搬家轴三次尝试都没触发，但失败点已定位到“部署漂移”

日期：2026-10-26　pod 同上；引擎 plain vLLM(892 块×128=114,176/卡)
负载：40 program × 8 轮 × 15 s、前缀 1500 词、heavy 0.7×3、`EXECUTOR_PLACEMENT=fill`

## 测到的数字

| 格 | 命中率 | 中位 | 重算 token | p50 | tpm | moves |
|---|---|---|---|---|---|---|
| even / pin-off | 0.857/0.856/0.859/0.857 | **85.7%** | 146k | 0.34 s | 151.7 | 0 |
| even / bytes-off | 0.856/0.857/0.857/0.859 | **85.7%** | 146k | 0.35 s | 151.6 | 0 |
| fill / off（挤满 a 卡） | 0.186/0.195/0.186 | **18.6%** | 1,557,960 | 2.16 s | 76.0 | 0 |
| fill / on（HOT=0.75） | 0.219/0.179 | 19.9% | 1,533,076 | 2.01 s | 75.4 | **0** |
| fill / on（HOT=0.60） | 0.231/0.194/0.202 | **20.2%** | 1,527,640 | 1.86 s | 76.2 | **0** |

## 两条结论

1. **放置质量是主要矛盾**：85.7% vs 18.6%，差 67 个点。这个差距远大于任何搬家策略能带来的收益（历史上最好的一次是 7.4% → 54%，且那是人为超容的急救场景）。
2. **搬家轴仍然没测成，原因是部署漂移**：跑在 `<server-dir>` 的那份代码是早期拷贝，`executor/app.py` 里没有 `[bal]` 调试打印、`_balance_once` 的异常被 `except Exception: pass` 吞掉；本地仓库后来加的 `ProgramTable.entries()`（`_demand_ratio` 依赖它）没有同步过去。所以每次 tick 抛 `AttributeError` 后被静默吞掉，`moves` 恒 0。

## 下一步（一行改动）

同步本地 `executor/{app,programs}.py` → `<server-dir>`，在 `_balance_once` 的守卫前加一行 `print("[bal]", usage, BALANCE_HOT, BALANCE_COLD)`，重启后跑 1 rep 看 held/gap；确认非零后再跑 3 rep × 2 臂。

## 教训（写进 EXPERIMENTS 的“方法学坑”）

**本地与服务端代码必须同源**：本仓库有三份拷贝（`program-executor/`(本地)、`<server-dir>`、`<server-dir>`），补丁只打到其中一份时，表现为“机制不生效”而不是“代码没跑”。今后每次真机前先 `md5sum` 比对两端的 `executor/*.py`。
