# 补丁归档（2026-10-24 会话）

升腾 pod（`<pod-name>`）是临时的，已失联两次、主机名换过一次。
本目录把当天打过的补丁与证据固化到本地 git，pod 重来时按这里重放。

| 文件 | 作用 | 状态 |
|---|---|---|
| `apply_store_worker_patch.py` | 补 vLLM `mooncake/store/worker.py` 的 `_repr_tensor`（NPU KV 缓存不是 `Tensor/list` 导致 AssertionError） | **实测有效**：补后 A/B 两引擎 90 s 内起来 |
| `store_cfg.example.json` | Mooncake store 客户端配置（`metadata_server` 必须带 `/metadata` 后缀） | 实测有效 |
| `launch_store_servers.sh` | master + http metadata server 的正确启动参数与验证命令 | 实测有效 |
| `router_progress_guard.md` | ThunderAgent `scheduler/router.py` 的“进展保证”（修暂停死锁） | **重建记录**，需在 pod 上比对原文后再套用 |
| `router_multibackend.md` | 去掉 controller `__main__.py` 的单 backend 限制 | 已在 `scripts/patch_controller_multibackend.py` 里，此处仅索引 |

## 应用顺序（pod 起来后）

```bash
# 1) store 通道（Python 层）
python3 patches/apply_store_worker_patch.py          # 幂等，会打印 PATCHED / already

# 2) 两个服务端
bash patches/launch_store_servers.sh

# 3) 引擎
#    A: ASCEND_RT_VISIBLE_DEVICES=0 MOONCAKE_CONFIG_PATH=store_cfg.json vllm serve ... --port 8110
#    B: ASCEND_RT_VISIBLE_DEVICES=1 MOONCAKE_CONFIG_PATH=store_cfg.json vllm serve ... --port 8111
```

## 验证判据（缺一不可）

1. `curl -s -X PUT -o /dev/null -w '%{http_code}' 'localhost:8081/metadata?key=t&v=1'` → **200**（不是 404）；
2. A 引擎 `/health` → 200；
3. A 发一条请求后 `Keys: >0`（master 日志）；
4. B 发同一条 prompt，`vllm:prefix_cache_hits_total` 增量 ≈ prompt 长度，`queries` 增量同量级。

截至归档时：1、2 通过，**3、4 均失败**（`Keys: 0`；B `queries +3505 / hits 0`）。
失败原因见 `docs/M5-store-channel.md`。
