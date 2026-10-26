# Step 2：轻度不均（搬运真的启动了）与 transfer 臂为何作废

日期：2026-10-25　pod `<pod-name>`

## 配置

40 program，600 词前缀（≈1050 token），`--skew-frac 0.85`（34 个钉到 a 卡，约 36k/47.6k 容量，两卡都不超容），8 轮，轮间 15 s。

## 数字

| 臂 | 命中率 | 重算 token | 吞吐 | p50 | p95 | 搬家 |
|---|---|---|---|---|---|---|
| recompute r1 | 69.37% | 102,314 | 153.0 tpm | 0.434 s | 0.764 s | 5 |
| recompute r2 | 66.88% | 110,347 | 153.0 tpm | 0.427 s | 0.891 s | 5 |
| transfer r1 | 18.65% | 270,828 | 82.4 tpm | 0.765 s | 1.107 s | 3 |
| transfer r2 | 14.29% | 287,150 | 82.1 tpm | 0.850 s | 1.123 s | 3 |

**transfer 臂的这组数字作废**——它不是"真搬运更差"，而是"真搬运没成功"。见下。

## 单 program 探针暴露的真相

一次完整迁移的响应体：

```
'warm': {'ok': True, 'capture_ms': 122, 'blocks': 15,
         'pull': {'ok': False, 'error': 'ReadTimeout: '},
         'fallback': 'recompute',
         'fallback_result': {'ok': True, 'warmed_prompt_tokens': 1920}},
'carrier': 'recompute'
```

即：capture 成功拿到 15 块的握手包，**pull 超时**，executor 静默回退到重放式预热（回退后目标卡首轮命中 0.933）。
所以 transfer 臂实际跑的是"尝试拉取 → 等超时 → 回退"，代价是每次迁移把窗口堵死几十秒，新请求全 503。

## 修掉的两个真 bug

1. **`kv_port` 冲突**：`KVTransferConfig.kv_port` 默认 14579，两台引擎都用默认值，于是 Ascend 的 side channel 端口相同（`side_channel_port = kv_port + dp_rank*tp*pp*pcp = kv_port`），互相对不上。已给小卡设 `kv_port: 14580`，两边 blob 的 `remote_port` 现在是 14579 / 14580。
2. **`blocks` 读数**：capture 的 `blocks` 用外层 list 长度算，嵌套结构下恒为 1；改为读 `num_prompt_blocks`，现在正确报 15。

## 仍然卡住的根因（决定性）

`vllm_ascend/distributed/kv_transfer/kv_p2p/mooncake_connector.py`：

- `side_channel_port = kv_transfer_config.kv_port + dp_rank*tp*pp*pcp`，`handshake_port = side_channel_port + device_index`
- 握手走 **ZMQ**：`make_zmq_path("tcp", side_channel_host, handshake_port)`，交换的元数据是 `{remote_handshake_port, remote_port_send_num, ...}`

而我们传的握手包是 vLLM 通用 MooncakeConnectorV1 的字段（`remote_host/remote_port/remote_engine_id/remote_block_ids`），**不含 Ascend 需要的 ZMQ 元数据**。所以对端连上来时看到非 JSON 内容，两侧日志都是：

```
SocketHandShakePlugin: failed to receive handshake message, malformed json format
```

手册测试（no/bootstrap-http/bootstrap-bare 三种字段变体）全部 60 s 超时，无一成功。

## 结论

- 路线甲的 **recompute handoff 可用**，且在轻度不均下把命中率从 84%（更早 900 词均匀）量到 69%（34/40 倾斜后仍能救回）——每次搬家 `balance_moves` 确实发生。
- 路线乙的 **executor 侧自发拉取**在当前 vllm-ascend 构建下不可用：需要按 Ascend 的 ZMQ 握手协议构造元数据（可直接 import `mooncake_connector` 里的握手助手），这不是配置项，是一段移植工作。
- M6-A 单对 p2p push 能跑通（1.82×），说明"能搬"，但那是官方 proxy 的请求形态；要把它做成 program 级迁移，还差 ZMQ 握手这一层。

## 可选下一步

1. 移植 ZMQ 握手：在 executor 里构造 `remote_handshake_port` / `remote_port_send_num`，或直接复用 `vllm_ascend` 的 `MooncakeConnector` 助手；跑通则 transfer 臂可重测。
2. 改用 push 形态（prefill 直接推给目标卡，目标卡处于 consumer 角色），代价是角色不对称。
3. 冻结 transfer，先把 recompute handoff + 容量墙写进报告交付。
