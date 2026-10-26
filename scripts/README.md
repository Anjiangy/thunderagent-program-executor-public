# scripts 里每个脚本是干什么的

跑的顺序不重要，它们互不依赖，都是"读 data/ 里的 json 或 csv → 出一张图/一张表"。

## 出图

| 脚本 | 干什么 | 要多久 | 产出 |
|---|---|---|---|
| `plot_m3.py` | 亲和 vs 轮询的剂量-反应双图 | 秒级 | `data/m3_affinity.png` |
| `plot_m4.py` | 预热式迁移 vs 不预热，三次跑 | 秒级 | `data/m4_kv.png` |
| `plot_m4_compare.py` | 我们 vs 裸 ThunderAgent 的吞吐/命中双图 | 秒级 | `data/m4_compare.png` |
| `plot_m5.py` | 持续到达场景下两臂各三次（带均值线） | 秒级 | `data/m5_admission.png` |
| `plot_overview.py` | 把三张图拼成一张"总览"给汇报用 | 秒级 | `data/m5_overview.png` |

这些都只依赖 matplotlib，不连服务器。

## 跑实验（要在服务器上，连真 vLLM）

| 脚本 | 干什么 | 要多久 | 注意 |
|---|---|---|---|
| `run_compare.sh` | 起两条臂（裸 TA / 我们）各跑一遍对比 | 每臂 2~10 分钟 | 需要 vLLM 已在 8022/8023；输出 `data/compare/*.json` |
| `summarize_compare.py` | 把上面那堆 json 合成一张 markdown 表 | 秒级 | 无 |
| `diagnose_pause_rate.py` | 采样 controller 的暂停压力，看它是不是在反复暂停/恢复 | 跟着一次跑同步 | 用来诊断"停住"这类问题 |

真正跑负载的 bench 在 `tests/` 里（`compare_bench.py`、`m3_affinity_bench.py`、`m4_kv_probe.py`、`m2_throttle_probe.py` 等），`scripts/` 只负责起停和汇总。

## 打补丁（改别人代码用的）

| 脚本 | 干什么 | 为什么需要 |
|---|---|---|
| `patch_controller.py` | 给上游 ThunderAgent 适配器注入 `program_id_hint` | 上游会把 `program_id` 从 body 里删掉，不注入就传不到 executor |
| `patch_controller_multibackend.py` | 去掉 controller 只允许一个 backend 的限制 | 想做"每卡一个 executor"的拓扑时必须改 |

两个都是幂等的，重复跑只会报 `applied=0 skipped=N`。改之前建议先 `git diff` 看一眼。
