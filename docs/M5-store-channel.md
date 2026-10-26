# M5 · Mooncake**Store** 通道（A 存 B 取）实验记录

日期：2026-10-24　机器：升腾 pod `<pod-name>`（2×910B2，vLLM 0.23.0，mooncake 0.3.11.post1）

## 一句话结论

**框架层打通，数据面没打通。** 两个引擎都能带着 `MooncakeStoreConnector` 起来并正常应答，
但 A 存不进 store（`Keys: 0`）、B 取不到（`queries +3505 / hits 0`）。
所以“1（A 能存）+ 2（B 能取）同时成立”的可行性证明 **未达成**。

## 打通的部分（可复现）

| 环节 | 事实 |
|---|---|
| master | `mooncake_master -rpc_port=9013 -enable_ha=false`，15 s 内 `role=leader, state=serving`；`netstat` 显示 **9013(master service) + 9003(admin)** 都在 LISTEN。**本 pod 内 `ss -lntp` 输出为空，会误判“没监听”，必须用 netstat。** |
| 元数据 | `mooncake_http_metadata_server --port 8081`；路由只有 `/metadata`（`mooncake/http_metadata_server.py:60`）。客户端必须配 `http://127.0.0.1:8081/metadata`，写根路径一律 404 → `Failed to initialize transfer engine, rc=-200`。 |
| Python 补丁 | `mooncake/store/worker.py::_repr_tensor` 断言 `isinstance(v, torch.Tensor | list)` 在 NPU 上必失败 → 改为递归取首个 Tensor + 调用点 `None` 跳过（见 `patches/apply_store_worker_patch.py`）。补后 **A `A_UP=90s`、B `B_UP=90s`，`/health` 200，请求 200**。 |

## 没打通的部分（决定性）

```
A 请求 200 之后：master 日志 `Keys: 0`
B 起后发同一 prompt：vllm:prefix_cache_queries_total +3505，prefix_cache_hits_total = 0
A/B 日志各 24 条：
  E ascend_direct_transport.cpp:342] mem addr:0xfff2f172f040 can not be recognized, try set to host mem.
  E transfer_metadata.cpp:472] Failed to register segment descriptor, name ... protocol ascend
```

因果链：**Ascend 传输层不认 vLLM-Ascend 分配的 KV 显存地址 → `register_buffer` 失败 → store 里没有 buffer → 存不进也取不到 → B 全量本地重算。**

这不是配置能绕的（`protocol` / `kv_buffer_device` / env 都试过），属于 C++ 传输层能力缺口。

## 与 P/D 通道的成本对比（为什么即使打通也未必值）

同机 2770 token，两种路径（数据见 `docs/M5-pd-spike.md`）：

| 路径 | turn1 | turn2 |
|---|---|---|
| 本地重算 | 1.099 s | 0.916 s |
| P/D 真搬块 | **1.302 s** | **1.015 s** |

**搬运本身就比重算慢 0.10~0.20 s**；store 还要多一次序列化/落盘，只会更慢。
因此 store 通道即使打通，也大概率打不过路线甲“预热重放”的 96~97% 命中（且预热发生在 tool call 停机期，不在关键路径）。

## 下次接着做（pod 起来后）

1. 判定“A 类（配置/内存类型）还是 B 类（真缺实现）”：
   `grep -rn "can not be recognized" / vllm-workspace 与 mooncake 源码`，看它用什么判定 device 内存
   （`aclrtPointerGetAttributes`？白名单？），再用 20 行程序喂一块 `torch_npu` 显存看返回值；
2. A 类：配通即可；B 类：需改 `ascend_direct_transport` 并重建 `.so`（aarch64 + ACL 工具链 + 版本须对这一版 0.3.11.post1，否则会打碎已跑通的 P/D 通道）；
3. 只有 `Keys > 0` 且 B 侧 `hits ≈ prompt 长度`，才把这条写成“成立”。
