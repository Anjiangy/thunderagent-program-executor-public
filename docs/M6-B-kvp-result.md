# M6-B 结果：executor 侧"真搬运迁移"打通（小规模探针）

日期：2026-10-25　pod：`<pod-name>`

## 改了什么

`mode=kv` 现在有两条载体，由 `EXECUTOR_MIGRATE_CARRIER` 选：

- `recompute`（旧行为）：在目标卡**重新预填充**一遍前缀；
- `transfer`（新）：在**源卡**要一份握手包（`_capture_kvp`），只改 pin，把这个包挂到 program 上；
  该 program 的**下一次真实请求**到达目标卡时注入 `do_remote_prefill`，让目标卡**从源卡拉块**。

关键点：取握手包的那次请求打在源卡上，源卡的 prefix cache 里已经有这段前缀，所以它是**命中**而不是重算——
代价落在"便宜的源卡"上，省下的是"目标卡的关键路径"。

新增 API 面：`ProgramEntry.pending_kvp`、`ProgramTable.set_kvp/take_kvp`（一次性消费，`to_dict()` 暴露
`kvp_ready`），`_do_migrate` 返回里多了 `carrier` 字段；`transfer` 失败会自动回落到 `recompute`（并在
`warm.fallback` 里留痕），所以打开这个开关不会让路由不可用。

## 端到端探针（单次）

环境：A=:8110（`kv_producer`）、B=:8111（`kv_consumer`），`MooncakeConnectorV1` + `kv_buffer_device=npu`；
executor :8100，`PLACEMENT=affinity`、`BALANCE=off`、`MIGRATE_CARRIER=transfer`。

| 步骤 | 结果 |
|---|---|
| program `mv1` 首轮落在 a | 0.464 s |
| `migrate` → b，`mode=kv`，carrier=transfer | `ok=True`，`capture_ms=125`，握手包已挂上（`kvp_ready=True`） |
| `mv1` 搬家后首轮落在 b | **0.453 s** |
| 对照 `co1`：先落 a（0.638 s），`mode=recompute` 搬到 b 后首轮 | **0.627 s**（b 上冷） |

⇒ 搬家后首轮比目标卡冷启动快 **1.38×**；且这条路径**没有在 b 上重算前缀**。

## 边界（必须写明）

1. **n=1**。这只是"机制通了"的证明，不是性能结论。
2. 探针前缀只有约 2.7K token，绝对差 ~170 ms 里还混着 HTTP/序列化开销；真正该量的是
   `docs/M6-migration-workload-plan.md` 里那个专场景（40 program × 4600 token × 8 轮 + 15 s 工具窗）。
3. `remote_block_ids` 是"每层的块 id 组"，我打印的是组数（1 组），不是块数；别把它读成"只搬了 128 token"。
4. 这套引擎是 producer/consumer 角色，双向都当 `kv_both` 才是常态拓扑；本探针只验证了 a→b 单向。

## 复现

```bash
# 两侧引擎：MooncakeConnectorV1 + kv_buffer_device=npu + prefill/decode 的 tp_size/dp_size
export EXECUTOR_MIGRATE_CARRIER=transfer
python -m uvicorn executor.app:app --port 8100
python tests/m6_kvp_probe.py
```
