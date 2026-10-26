# 步骤 1 状态：把两张卡分别暴露给 controller（path B）

## 改了什么

`scripts/patch_controller_multibackend.py`：给自带的薄适配器去掉"只允许一个 backend"的硬限制
（`__main__.py` 的 `len(args.endpoint) != 1` 报错、`upstream_adapter.py` 的 `backends=[backend]` 与
`assert list(router.backends)==[backend]`）。上游 ThunderAgent 的 17 个文件 SHA256 仍然全部通过——
被改的只有我们自己的适配器，不是钉住的上游核心。

幂等：第二次执行 `applied=0`。

## 跑起来的形态（已真机验证）

```
client → controller :9000（两个 backend：8101 / 8102）
           ├── executor-a :8101 → vLLM 8022（device0）
           └── executor-b :8102 → vLLM 8023（device1）
```

- `/migration-identity` 返回 `backends` 长度 **2**；
- 每个 backend 的容量由 executor 的 `/metrics` 报 `block_size=128, num_gpu_blocks=892` → 单卡 114176，
  与控制器 `has_capacity` 的口径一致（不再是"集群 228352"）；
- 小规模通路验证：4 program × 2 轮 = 8 请求，3.5 s，失败 0，命中 0.33（短前缀，符合预期）。

## N=40 还在撞的地方

per-card 形态下 N=40（40 program × 约 6.2k ≈ 248k > 228k）**仍然没有在 900 s 内跑完**。
controller 日志显示 42 次 pause / 38 次 resume，单卡 active 常年 105k~112k（上限 114176）——
这次是**真实的超订**（每张卡都满），不是聚合口径造成的"卡空着、controller 满着"。
也就是说：步骤 1 修掉了那个**错误的停住**，但没有修掉"总需求超过总容量"时的轮转抖动。

## 顺带发现的方法学问题（必须先修，否则数字不可信）

`tests/compare_bench.py` 里 `rng = random.Random(f"{label}:{seed}")`，即 **prefix 由 label+seed 决定**，
而它不清 prefix cache、也不重启 vLLM。于是用同一个 label 重跑会命中上一次的缓存：
实测同一条命令重跑 N=24 得到 **702 tpm / 0.9889 命中**——这个数字是假的（缓存已热）。

规则：每次测量换一个新 `--seed`（或重启 vLLM），并且两臂用同一个 seed（label 会进 rng，
所以两臂要么同 label 不同端口，要么把 label 从 rng 里摘出去）。这条要写进 M4 对比文档，
之前的 24/32 两档是冷缓存跑的（命中 0.73，与 M3 一致），但 40 档的 ours 结果需要重测。
