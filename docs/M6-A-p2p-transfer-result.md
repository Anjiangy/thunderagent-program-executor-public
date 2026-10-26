# M6-A 结果：跨卡 KV 搬运在本平台通了（p2p 通道）

日期：2026-10-25　pod：`<pod-name>`（2×910B2）

## 结论

**两卡之间真的能搬 KV**，而且搬比在目标卡重算快约 **1.8×**：

| 轮次 | prompt tokens | 目标卡冷重算 TTFT | 从源卡搬到目标卡 TTFT | 加速 |
|---|---|---|---|---|
| t1 | 1466 | 0.228 s | 0.118 s | 1.93× |
| t2 | 1466 | 0.197 s | 0.110 s | 1.79× |
| t3 | 1466 | 0.196 s | 0.113 s | 1.74× |

均值：**0.207 s → 0.114 s，1.82×**（数据 `data/m6_p2p_transfer.csv`，图 `data/m6_p2p_transfer.png`）。

## 通道与启动参数（可复现）

连接器是 **`MooncakeConnectorV1`**（不是 store，也不是 LMCache）。两个引擎：

```bash
# 源（prefill 角色）
--kv-transfer-config '{"kv_connector":"MooncakeConnectorV1","kv_role":"kv_producer",
  "kv_buffer_device":"npu",
  "kv_connector_extra_config":{"prefill":{"tp_size":1,"dp_size":1},
                               "decode":{"tp_size":1,"dp_size":1}}}'
# 目标（decode 角色）：同上，kv_role 换成 kv_consumer
```

两个必须项，缺一个都起不来：

1. `kv_buffer_device: "npu"`；
2. `kv_connector_extra_config` 里 **prefill 和 decode 两组都要有** `tp_size`/`dp_size`
   （`mooncake_connector.py:2068` 与 `2110` 各有 `assert "tp_size" in ..._parallel_config`）。

## 协议：不需要手工 bootstrap

之前卡住是因为我按老 proxy 的手工方式构造 `remote_bootstrap_addr=http://127.0.0.1:8998`，而 8998 上
没有任何监听。实际上这个版本**由源端在响应里直接返回握手参数**：

源端请求带
```json
{"kv_transfer_params": {"do_remote_decode": true, "do_remote_prefill": false, "transfer_id": "xfer-xxx"}}
```
响应里的 `kv_transfer_params` 就包含了
```json
{"remote_block_ids": [[1..9]], "remote_engine_id": "...", "remote_host": "<pod-ip>",
 "remote_port": 14579, "num_prompt_blocks": 9, "last_token_id": 5430,
 "remote_multi_nodes_meta_mapping": {"0": {"host": "...", "engine_id": "...", "handshake_port": 14579}}}
```
把这份 dict 原样带到目标端请求（再加 `do_remote_prefill: true`），目标端就会去拉。

## 旁证

- 目标端日志：`Connected to segment: <pod-ip>:20117`（源端传输段），且两个引擎的 KV 内存段都
  `register mem addr:... mem type:device`（设备内存注册成功，没走 host 注册）。
- 两侧周期性打印 `SocketHandShakePlugin: failed to receive handshake message, malformed json`，
  **但不影响搬运**（请求 200、TTFT 正常）——看起来是后台元数据探测打到了非 JSON 端点，属噪声，
  后续可单独查。

## 边界（不要过度解读）

1. 这证明的是 **P/D 式“源卡预填充 → 目标卡装载”** 通；**还没证明**“program 在等工具时被迁到另一张卡”。
   后者才是 M6 的命题，需要 executor 参与调度（见下节）。
2. `usage.prompt_tokens_details.cached_tokens` 在本版本恒为 `None`，所以判据用的是 TTFT + 日志，
   不是 cached 计数。
3. 与 store 通道的结论不冲突：store 的元数据（lookup）通了、数据面（adxl engine connect）没通；
   p2p 这条是另一条独立通道，现在通了。

## 下一步（M6-B：把它接进 executor，变成真迁移）

executor 侧的最小改法（复用现成 `/v1/programs/{pid}/migrate`）：

1. program 首次在 A 上跑时，用它的前缀在 A 上做一次 `do_remote_decode` 预填充，把返回的
   `kv_transfer_params` 存进 program 表（`pending_kvp`）；
2. 迁移到 B 时**不预热、不重算**，只改 pin 并把 `pending_kvp` 挂上；
3. 该 program 的下一次真实请求，executor 把 `pending_kvp` 注入 body（`do_remote_prefill: true`），
   B 直接从 A 拉 —— 这才是“让工作换个地方”；
4. 迁移完成后清空 `pending_kvp`，避免重复注入。

然后在 M6 的专场景负载（40 program × 4600 token × 8 轮 + 15 s 工具等待窗口）上对比
“重算式交接 vs 真搬运交接”，判据见 `docs/M6-migration-workload-plan.md` 的 P1–P4。
