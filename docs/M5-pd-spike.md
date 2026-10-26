# M5 — the block channel exists on this platform

Route B assumed KV could be moved between cards as a *unit*. Earlier I judged that
impossible here, because `vllm_ascend` ships no KV connector and importing it from
our process died. That judgement was wrong. This note records the spike that
overturned it, and the first number for program-level reuse.

## 1. What was deployed

Two vLLM instances, one card each, joined by the stock `MooncakeConnector`, with
the upstream P/D proxy in front (`scripts/pd_spike.sh`):

| role | card | port | kv-transfer-config |
|---|---|---|---|
| prefill (producer) | 0 | 8102 | `{"kv_connector":"MooncakeConnector","kv_role":"kv_producer"}` |
| decode (consumer) | 1 | 8103 | `{"kv_connector":"MooncakeConnector","kv_role":"kv_consumer"}` |
| proxy | — | 8104 | `mooncake_connector_proxy.py --prefill :8102 8998 --decode :8103` |

Both engines print, unprompted:

```
Creating v1 connector with name: MooncakeConnector and engine_id: ...
Initializing Mooncake Transfer Engine worker ...
The Mooncake Transfer Engine is using rdma as its protocol.
Mooncake Bootstrap Server started at 0.0.0.0:8998
MooncakeConnector setting KV cache layout to HND for heterogeneous TP-safe KV transfer.
Registering KV_Caches. use_mla: False
```

No patching, no CUDA→HCCL port, no kernel work: `mooncake_transfer_engine_npu`
picks RDMA on its own.

## 2. Proof that KV crossed cards

One request through the proxy (`prompt_tokens = 812`, `completion_tokens = 8`, HTTP 200):

```
{"id":"cmpl-cc3756ec-...","choices":[{"text":" Text. ...","finish_reason":"length"}],
 "usage":{"prompt_tokens":812,"total_tokens":820,"completion_tokens":8}}
```

Three independent traces:

1. the producer log shows the RDMA session to the consumer segment
   (`Connected to segment: <pod-ip>:20725`);
2. the decoder never ran a prefill for those 812 tokens — they arrive as already
   computed blocks;
3. `kv_load_failure_policy='fail'` is the default, so a failed load would have
   returned an error instead of a completion.

Caveat kept on the record: one handshake attempt logged
`SocketHandShakePlugin: failed to receive handshake message, malformed json format`
20 s before the real connection. It is a probe packet, not the data path.

## 3. First program-level number

`tests/pd_program_turns.py` sends two turns against the same pair, 2763-token
prompts, `max_tokens=8`, and reads both engines' `/metrics` before and after each
turn. Raw deltas below are doubled because `prefix_cache_*` is exported once per
label set; divided values are in brackets.

| arm | turn 2 wall | prompt | prefill recomputed | prefill reused | decode recomputed |
|---|---|---|---|---|---|
| reuse (prefix resident) | 0.982 s | 2759 | 71 [142/2] | 1344 [2688/2] | 0 |
| cold (prefix not resident) | 1.263 s | 2759 | 2759 [5518/2] | 0 | 0 |

So a turn whose prefix is already materialised costs **71 new prefill tokens
instead of 2759 — about 40× less prefill work**, and 0.98 s instead of 1.26 s.

## 4. What this does *not* show yet

- It is prefix reuse **on the producer**, not a program's blocks being moved.
  The decoder reports the transferred tokens as cache hits in both arms, which is
  consistent with "received, not recomputed", but it cannot distinguish
  "blocks were here already" from "blocks just arrived".
- The `cold` arm's first turn was contaminated by the `reuse` arm's prefix
  (`hits = 2688` instead of 0), so cross-arm ordering still needs dedicated
  prefixes per arm.
- Nothing here is a comparison against ThunderAgent; it is a capability probe.

## 5. Why it matters for route B

The missing piece for block-level program migration was never the algorithm, it
was the transport. The transport exists. What route B still needs:

1. a program→blocks map on the executor side (which request ids / block hashes
   belong to a program);
2. a migration request that names a source instance and a target instance and
   moves exactly those blocks (the connector already exposes send/recv by
   segment);
3. a cost model that decides when a move beats a recompute — with 40× prefill
   leverage at this prompt length, that bar is lower than I assumed.

## 6. Cost table: two arms on the live P/D pair (2026-10-24, 2770-token prompts)

`tests/pd_arm_bench.py` — `recompute` posts straight to the decode engine (it prefills
itself), `transfer` goes through the proxy (prefill engine builds blocks, decode receives
them). Each arm sends the same prefix twice; arms use different salts.

| arm | turn | wall | tokens | who prefilled |
|---|---|---|---|---|
| recompute | 1 | 1.099 s | 2768 | decode (2768 of them) |
| recompute | 2 | **0.916 s** | 2768 | decode: 80 new, 1344 reused |
| transfer | 1 | 1.302 s | 2770 | prefill 2770, decode 0 new |
| transfer | 2 | **1.015 s** | 2770 | prefill 82 new + decode 41 new |

Counters are exported once per label set, so raw deltas are doubled; values above are halved.

**Reading**: with the block channel available, the *transfer* arm is **slower than local
recompute** — +0.20 s on the first turn and +0.10 s on the second for the same 2.7k-token
prompt. It is not free: the producer must prefill before anything can be sent.

So the P/D channel proves blocks *can* move, but as a migration substitute it is a net
loss today. What would change the arithmetic is a one-sided move (store → load) where the
source does not recompute — see §5.2.

## 7. Store connector: attempted, blocked before it started

`MooncakeStoreConnector` needs `MOONCAKE_CONFIG_PATH` (written), an HTTP metadata server
(`mooncake.http_metadata_server`, started on :8080) and a store master. This container
exposes only `/dev/davinci2` and `/dev/davinci7` (mapped to 0 and 1), both of which were
hosting the P/D pair; after killing them the NPU memory had not been released within the
probe window, so the engine died on
`Free memory on device (14.4/60.96 GiB) ... less than desired GPU memory utilization (0.5)`.
The store connector itself was never reached, so this is **not** evidence that it cannot
work. Next attempt should run it in place of the P/D pair, on a card that has been idle
long enough to show ~60 GiB free.