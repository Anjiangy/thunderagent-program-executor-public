# Route B store channel - status log (2026-10-25)

## What is proven
- `MooncakeStoreConnector` initialises on NPU once `mooncake/store/worker.py::_repr_tensor`
  accepts the tuple/list that vLLM-Ascend hands over (patch: `patches/apply_store_worker_patch.py`).
- **Write side works**: engine A (card0, :8110) served a request and the master counted
  `Keys: 27`; after B's request `Keys: 43`, i.e. both engines stored their own KV blocks.
- So `ascend_transport.so` *does* handle device memory: it contains
  `aclrtPointerGetAttributes`, `adxl::MemType`, `aclrtReserveMemAddress`/`aclrtMallocPhysical`/`aclrtMapMem`.
  The `can not be recognized, try set to host mem` line is a per-address fallback, not missing support.
  => **Class A**: no C++ change, no .so rebuild.

## What is not proven
- **Read side**: engine B (card1, :8111) re-computed everything (`B_dq=3505`, `hits=0`).
  A probe line was inserted into `worker.py::lookup` right after `store.batch_is_exist`:
  `# [lk-probe] tok=... hashes=... keys=... hit=x/y first=<pool key>`
  That single line decides the next move:
  * `hit>0` -> the store has the keys, the bug is in the token accounting;
  * `hit=0` -> key namespace mismatch between write and read (compare `first=` with the writer's key).

## Why the probe has not produced output yet
Two `mooncake_master` instances competed for `:9013`; the new one logs
`bind port 9013 ... Address already in use` and stays `role=standby`, so the store client never
connects and the engine core hangs at init (`health=000`, no ValueError/AssertionError).

## Next three steps (environment only, no code)
1. `netstat -lntp | grep -E '9013|8081'`, kill every PID found (they survive `pkill -f mooncake_master`
   when started via a wrapper), re-check the ports are free.
2. Start exactly one `mooncake_master --port=9013 --enable_http_metadata_server=true
   --http_metadata_server_port=8081`, wait for `"role":"leader"` + `"service_ready":true` (~20 s).
3. Start engine B (card1, `--gpu-memory-utilization 0.35`, leftover engine cores eat ~37 GiB),
   POST the same prompt, read the `[lk-probe]` line.
## 7. 根因：第一块的 parent hash 是每进程随机的（2026-10-25 定案）

### 现象
同一个 prompt、同一串 token、同样的 block_size=128，A 写进 store 的 27 个 key 在 B 上算出 27 个**全不相同**的 key（`hit=0/27`），且 A 自己跨运行也漂移（`4a965614` → `6f44d4aa`）。

### 根因
`vllm/v1/core/kv_cache_utils.py:101-111`（`init_none_hash`）：

```python
hash_seed = os.getenv("PYTHONHASHSEED")
if hash_seed is None:
    NONE_HASH = BlockHash(os.urandom(32))
else:
    NONE_HASH = BlockHash(hash_fn(hash_seed))
```

`NONE_HASH` 是**第一块的 parent hash**，而每个块 hash 都是 `sha256(parent, tokens)` 链式算出来的。`PYTHONHASHSEED` 未设置时它取 32 字节随机数 ⇒ 每个进程的整条 hash 链都不一样。所以写侧 key 天生不可复现，读侧再努力也命中不了 —— 这不是 store 的 bug，也不是 connector 的 bug。

### 证据（key 级，CPU，无需起引擎）
同一段 4×128 token，两次独立进程：

| PYTHONHASHSEED | 进程 1 | 进程 2 | 一致 |
|---|---|---|---|
| 未设置 | `909eb54b,fd023275,b9692633,481501cc` | `1d5ac233,835a968b,b619e3b9,a0ccc610` | ❌ |
| `0` | `d5a79ef9,b7fb2ceb,415d2419,ab6de795` | `d5a79ef9,b7fb2ceb,415d2419,ab6de795` | ✅ |

复现脚本：服务器 `<server-dir>`、`<server-dir>`。

### 修法
**不改哈希算法，只固定种子**：所有引擎（A、B、以及将来任何共享 store 的实例）启动时统一
`PYTHONHASHSEED=0`。无需改 vLLM 源码、无需重编 `ascend_transport.so`。

### 待验证
设好后重启 A/B + store，预期 `[lk-probe] hit=27/27`、`B_hits>0`，从而闭合「A 存 B 取」。这也解释了此前 store 写进去的 347 MB 为什么谁都没读到。
