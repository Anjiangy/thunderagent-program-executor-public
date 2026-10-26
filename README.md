# program-executor

> **公开版说明**：内网主机名 / IP / 端口 / 服务器路径 / 模型路径均已替换为占位符（`<ascend-host>`、`<server-dir>`、`<model-path>` 等），实验数字与代码未做任何修改。


> **术语说明（2026-10-25）**：本文里的 `mode=kv` 动作是 **recompute handoff（重算式交接）**，
> 不是 KV migration：它不搬运任何 KV block，只是在 program 停顿（调工具）期间把该 program 的
> prompt 在目标卡上重新 prefill 一遍，再把 pin 指过去。收益是"把这次 prefill 挪出关键路径"
> 与"重新平衡驻留"，代价是多算一次前缀；跨卡没有任何数据流动。
> 真迁移（block copy）在本平台不可用，三条连接器的证据见 `docs/route-B-store-status.md`。

一个很窄的问题：**同一个 agent 程序的多轮请求，能不能一直落在同一张卡上，让它的前缀缓存别反复失效。**

vLLM 自己只认"这一次请求"，它不知道"这三轮属于同一个程序"。所以只要前面有一个负载均衡器在轮询，同一段上下文就会在两张卡之间来回搬——每搬一次，前缀缓存就白算一次。这个仓库就是补这一层：记住每个 `program_id` 现在住哪张卡，并把两张卡的容量合成一个数字报出去。

## 三个词，先认识一下

| 词 | 白话 |
|---|---|
| **program** | 一个多轮任务，客户端用 `program_id` 标它。属于同一个 program 的请求，上下文是接续变长的 |
| **pin（钉住）** | 这个 program 被分到哪张卡，就一直在那张卡上，不轮询 |
| **recompute handoff（重算式交接）** | 搬家前先把它的 prompt 在新卡上跑一遍（`max_tokens=1`），搬完第一个 turn 就是缓存命中 |

## 30 秒验证（本机，不需要 GPU）

```
python -m pytest -q
```

14 个测试，只看逻辑，不连任何服务。这条命令过了，说明放置、迁移状态机、指标聚合这三块代码是对的。

## 真机怎么跑起来

前提：一张卡上已经有一座 vLLM，端口假设 8022，另一张 8023，都开了前缀缓存。

```
# 1) 起 executor（它同时看两张卡）
export EXECUTOR_BACKENDS="a=http://127.0.0.1:8022,b=http://127.0.0.1:8023"
export EXECUTOR_PORT=8100
export EXECUTOR_PLACEMENT=affinity      # affinity | roundrobin
export EXECUTOR_BALANCE=skew            # skew | off
export EXECUTOR_PLACEMENT_CAP=0.85      # 超过这个使用率的卡不再接新 program
python -m uvicorn executor.app:app --host 0.0.0.0 --port 8100

# 2) 确认它看见了两张卡、容量是合起来的
curl -s localhost:8100/healthz
curl -s localhost:8100/metrics | head
#   vllm:cache_config_info{block_size="128",num_gpu_blocks="1784"}
#   1784 = 892 × 2，这就是"集群口径"

# 3) 发一个带 program_id 的请求，再发一次，看它落哪
curl -s localhost:8100/v1/programs/<pid>
```

## 六个路由

| 路由 | 干什么 |
|---|---|
| `/healthz` | 活着没、看见几张卡、当前放置/平衡配置 |
| `/metrics` | **集群口径**的 Prometheus 指标（`num_gpu_blocks` 是两张卡之和） |
| `/v1/chat/completions`、`/v1/completions` | 转发给 vLLM，顺手记下 program 的 prompt |
| `/v1/programs`、`/v1/programs/{pid}` | 看/删 program 表 |
| `/v1/programs/{pid}/migrate` | 把它迁到另一张卡（`mode=kv` 会先预热） |
| `/v1/models` | 透传 |

## 它到底值多少（都是真机测的，不是估的）

**1. 只有当单卡装不下时，亲和才值钱**

同一批负载，只换放置策略：

| 并发 program | 亲和命中率 | 轮询命中率 | 亲和吞吐 | 轮询吞吐 |
|---|---|---|---|---|
| 16（每卡装得下） | 89.5% | 83.3% | 417 tpm | 381 tpm |
| 32（开始溢出） | 81.3% | 60.1% | 556 tpm | 351 tpm |
| 40（明显溢出） | 85.5% | 53.0% | 704 tpm | 372 tpm |

N=16 两臂打平，是因为轮询时每卡 73K 也塞得进 114K；一旦塞不进，轮询就掉、亲和还稳。

**2. recompute handoff（重算式交接）确实能把"搬家"变成"命中"**

同一 program 迁到另一张卡，迁完第一个 turn 的命中率：

| 跑次 | `mode=kv`（先预热） | `mode=recompute`（不预热） |
|---|---|---|
| 1 | 96.8% | 0% |
| 2 | 97.0% | 0% |
| 3 | 97.3% | 0% |

**3. 跟裸 ThunderAgent 比，是"不输"，不是"赢"**

| 并发 | 裸 TA 吞吐 / 命中 | 我们（controller + executor） |
|---|---|---|
| 24 | 309 tpm / 73.4% | 307 tpm / 73.3% |
| 32 | 331.6 tpm / 73.4% | 331.1 tpm / 73.5% |
| 40 | 187.4 tpm / 41.8% | 当时"停住"没跑完 |

第三行那个"停住"，后来查清是我们自己的缺陷（暂停的 program 没有恢复进展保证，卡空着而 controller 满着），已用一行进展保证修掉，修完后同一负载 72/72 全成功。所以这张表**不能**用来断言"我们更好"，只能说明在容量内两者持平。

## 三个必须知道的坑

1. **命中率有理论上限**：`(T-1)/T`。T 轮对话里第一轮必然全 miss，所以 3 轮的上限是 66.7%、8 轮是 87.5%。凡是 3 轮就跑出来的"65% 左右"，基本是在贴上限，两臂差别都在噪声里。
2. **这里没有真正搬 KV block**。升腾上可用的搬运通道不存在（`vllm_ascend` 导入即崩），所以 `mode=kv` 是"在新卡上重放一次 prefill"，代价是一次前填充，好处是这次前填充可以发生在 program 等工具返回的空档里。
3. **绝对数字不能跟论文对比**。模型、卡、并发规模都不一样，只能比趋势和倍数。

## 想往下读

`docs/INDEX.md` 是按"我想知道什么"排的索引，`scripts/README.md` 是每个脚本干什么、跑多久、产出哪个文件。
