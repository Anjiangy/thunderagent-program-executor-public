# M1 实测结果（program-executor）

跑在升腾 `<ascend-host>`，两个 vLLM 实例（`8022` → device 0，`8023` → device 1，
Qwen2.5-14B-Instruct，block_size 128，各 892 块）。

## 1. 逻辑单测

```
python -m pytest tests/ -q      ->  10 passed
```

覆盖：Prometheus 文本解析（含 HTML 404 降级为 unhealthy）、按容量加权的集群聚合、
渲染回环（渲染出来的文本必须能被自己的解析器读回同样数字）、pin 表（新建/保亲和/
宿主机宕机时 failover/迁移生命周期/同目标拒绝）。

## 2. 进程级冒烟

```
GET  /healthz   -> {"ok":true,"healthy":["a","b"],...}
GET  /metrics   -> vllm:cache_config_info{block_size="128",num_gpu_blocks="1784"} 1.0
                   vllm:kv_cache_usage_perc{instance="cluster"} 0.00056
```

`1784 = 892 × 2`，正是两个实例的块数之和；这是 controller 需要的「一个大引擎」视角。

## 3. 程序亲和与疏散（真实请求）

| 动作 | 结果 |
|---|---|
| `program_id=p1` 连发两次 | 两次都落到 `a`（`x-executor-instance: a`） |
| `program_id=p2` | 落到 `a`（当时 a 更空） |
| `POST /v1/programs/p1/migrate {target:b, mode:recompute}` | `200`，`from=a, to=b, waited_ms=0` |
| 迁移后再发 `p1` | 落到 `b` ✔ |
| `mode=kv` | `501`，明确提示 route B 未实现 |
| 请求头 `x-program-id: p3` | 正常路由并被记录 |
| 未知 target / 未知 program | `400` / `404` |

## 3b. 新 program 的铺开

vLLM 自己的计数器不知道「一个实例上钉了几个 program」，所以两个空闲实例在原排序下看起来
完全一样，新 program 会一直堆到同一个实例上。排序键改为
`(钉住的 program 数, running, kv_usage, name)` 之后：

```
spread-0 -> a   spread-1 -> b   spread-2 -> a
spread-3 -> b   spread-4 -> a   spread-5 -> b
placement: {'a': 3, 'b': 3}
executor:programs_pinned{instance="a"} 3
executor:programs_pinned{instance="b"} 3
```

这条 `executor:programs_pinned` 指标也一起暴露出去，方便观察「program 是不是真的铺开了」。

## 4. 与 controller 的契约对齐（关键）

用 ThunderAgent **真实的**解析器（`ThunderAgent/backend/vllm_metrics.py`，绕开包
`__init__` 用 importlib 单独加载）读本服务输出的 `/metrics`：

```
VLLMCacheConfig(block_size=128, num_gpu_blocks=1784)   -> capacity 228352 tokens
VLLMMetrics(num_requests_running=0, kv_cache_usage_perc=0.0,
            prefix_cache_queries=5407, prefix_cache_hits=2816, hit_rate=0.5208)
```

即：controller 端**一行不用改**就能拿到正确的集群容量口径。

## 5. 已知边界（M1 不做的事）

- `mode=recompute` 只改 pin，下一轮在目标实例重算 prefill。省的是排队/抢占，不是算力。
- 迁移窗口内该 program 的新请求返回 `503 + Retry-After: 1`；controller 侧会把它记为一次失败，
  M2 接 controller 时需要在 `update_program_before_request` 里识别 `503` 并退避（否则台面数字
  会掺进这些 503）。
- 集群口径只保证「总量」正确，不保证「单个实例还有空位」。若 controller 按集群口径一次放行
  太多 program，仍可能撑爆某一个实例——这是 M3 要观测的点。
