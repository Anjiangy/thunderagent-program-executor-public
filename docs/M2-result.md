# M2 结果：真 controller 接上 executor

日期：2026-10-23　机器：`<ascend-host>`（Ascend 910B2 × 2，device 0/1）

## 拓扑

```
client ──/v1/chat/completions──▶ controller :9000      （ThunderAgent 薄适配器，钉 7ddc861）
                                      │
                                      │  /v1/chat/completions（同一条路径，program 身份带在 body 里）
                                      ▼
                                program-executor :8100   （集群 /metrics + program 亲和 + recompute 迁移）
                                      │
                        ┌─────────────┴─────────────┐
                        ▼                           ▼
                  vLLM :8022 (device 0)       vLLM :8023 (device 1)
                  892 blocks × 128            892 blocks × 128
```

## 暴露出来的真接缝

M2 的第一轮请求就撞墙：经 controller 发的 `program_id=m2-alpha`，在 executor 的
`/v1/programs` 里全部显示为 `default`。

根因（两处叠加）：

1. `ThunderAgent/scheduler/vllm_request_processor.py:82 remove_program_id()` 在转发前
   把 `program_id`（含 `extra_body.program_id`）从 body 里删掉；
2. `client.stream("POST", url, json=payload)` 只发 body，**不转发任何请求头**。

这在上游是正确设计——它自己决定 program 落在哪，下游不需要知道。但本方案里
"program 落在哪"正是 executor 的职责，身份被抹掉后亲和就退化成全量随机。

## 修法：一个上游不认识的字段

不修改钉住的 upstream 文件（SHA256 清单仍然通过），只在我们自己的薄适配器
`upstream_adapter.py` 里加一层：把 program 身份写进 `program_id_hint`，随 body 一起
转发；executor 认它、并在请求进 vLLM 前删掉它。

- `scripts/patch_controller.py`：幂等地给 `upstream_adapter.py` 注入
  `_with_program_hint(request, payload)`，用一个携带新 body 的 `Request` 交给上游
  官方 handler。解析顺序与上游一致：`program_id` → `extra_body.program_id` →
  `x-session-id` → `default`。
- `executor/app.py`：`extract_program_id` 最高优先级读 `program_id_hint`；
  `strip_program_fields` 把它和 `program_id`/`session_id` 一起删掉，vLLM 侧无感。

选这个方案而不是"补丁上游删除逻辑"的原因：钉住的上游版本是这套东西的信任基础
（17 个文件逐个 SHA256 校验），一旦改动就再也证明不了"我们复用的是官方实现"。

## 部署

```bash
# 上游包按 commit 逐文件导出成 LF（工作树 core.autocrlf=true 会污染成 CRLF，SHA256 会全错）
for f in $(git -C ThunderAgent ls-tree -r --name-only 7ddc861); do
  git -C ThunderAgent cat-file blob 7ddc861:$f > <server-dir>$f
done

python3 scripts/patch_controller.py            # 注入 program_id_hint
PYTHONPATH=<server-dir>:<server-dir> \
  python3 -m thunderagent_controller_mod --endpoint http://127.0.0.1:8100 \
    --listen 0.0.0.0:9000 --enable --scrape-interval 2.0

EXECUTOR_BACKENDS="a=http://127.0.0.1:8022,b=http://127.0.0.1:8023" \
  python3 -m uvicorn executor.app:app --host 0.0.0.0 --port 8100
```

controller 启动日志确认它把 executor 当成**一个**引擎：

```
Fetched cache config for http://127.0.0.1:8100: block_size=128, num_gpu_blocks=1784,
  total_capacity=228352
Started scheduler loop (interval=2.0s)
runtime_identity {"implementation":"pinned-official-thin-adapter", "upstream_commit":"7ddc861…",
  "loaded_sources": [17 files all verified]}
```

## 验证

### `tests/m2_controller_smoke.py`（12/12 PASS）

| 检查 | 结果 |
| --- | --- |
| executor 广告的是两张卡之和 | 1784 blocks × 128 |
| controller 看到的是一个引擎 | 228352 = 892×128×2 |
| program 请求成功 | 200 |
| 响应仍是标准 OpenAI JSON | 有 `choices` |
| controller 记录了 program | programs_count ≥ 1 |
| **executor 按名字认识 program** | `{'m2-alpha': 'b'}` |
| 两个 program 分居两个实例 | alpha≠beta |
| 第二轮保持 pin | b → b |
| 无 program_id 的普通请求 | 200 |
| hint 不漏给下游 | ✓ |
| recompute 迁移 | `from=b to=a` |
| 迁移后下一次落新实例 | a |

### `tests/m2_throttle_probe.py`（节流真的发生了）

30 个 program × 约 9000 token ≈ 270K > 228352 容量：

```json
{"programs": 30, "approx_tokens_each": 9000, "cluster_capacity": 228352,
 "status_codes": {"200": 30}, "wall_seconds": 24.3,
 "peak_paused": 11, "peak_reasoning": 30}
```

- 峰值 **11 个 program 被暂停**在 controller 的等待队列里；
- 30 个请求最终全部 200——容量释放后按 BFD 依次恢复；
- 说明容量账本经 executor 聚合后仍然自洽：controller 认为的"容量"= 两张卡之和，
  而不是一张卡。

## 已知边界

1. **hint 依赖适配器补丁**：不走薄适配器（直连 upstream `ThunderAgent` 包）时
   program 身份仍会被剥离。要彻底干净，得让上游支持向下游透传 program 身份。
2. **`program_id_hint` 是私有约定**：它只在本仓库与 executor 之间成立；如果哪天上游
   加了同名或同语义字段，需要重新对表。
3. **controller 的 `/v1/models` 仍是 404**（适配器只包 `/v1/chat/completions`），
   取 model id 要从 executor 拿。
4. **迁移仍是 recompute**：`mode=kv` 返回 501，KV 搬运留给路线乙。

## 文件

```
scripts/patch_controller.py        新增（适配器补丁，幂等）
executor/app.py                    +program_id_hint 解析与剥离
tests/m2_controller_smoke.py       新增（12 项真机检查）
tests/m2_throttle_probe.py         新增（节流观测）
docs/M2-result.md                  本文
```
