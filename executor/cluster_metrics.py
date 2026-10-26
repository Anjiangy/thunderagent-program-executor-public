"""Cluster-scoped Prometheus view over N vLLM instances.

The controller (ThunderAgent / request-throttling-controller) models capacity
with one engine's maths:

    KV capacity (tokens) = block_size * num_gpu_blocks
    KV used     (tokens) = kv_cache_usage_perc * KV capacity

With two instances that view is wrong unless it is aggregated. This module folds
per-instance numbers into one snapshot and renders it with exactly the metric
names and label shapes the controller's parser expects.

Aggregation is weighted by capacity, never averaged:

    num_gpu_blocks      = sum(num_gpu_blocks_i)
    kv_cache_usage_perc = sum(used_blocks_i) / sum(num_gpu_blocks_i)

Unlike the Llumnix variant, prefix-cache counters ARE exposed here: we scrape
real vLLM instances, which report them per engine, so they aggregate cleanly.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List

READY_HELP = "cluster snapshot is not ready (no KV geometry yet)"


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def aggregate_backends(backends: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold per-instance dicts into a single cluster snapshot."""
    total = used = running = waiting = 0
    pq = ph = pt = gt = pre = 0.0
    block_sizes: List[int] = []
    healthy = seen = 0

    for b in backends:
        if not b:
            continue
        seen += 1
        if not b.get("healthy", False):
            continue
        healthy += 1
        bs = _as_int(b.get("block_size"))
        blocks = _as_int(b.get("num_gpu_blocks"))
        total += blocks
        used += _as_int(b.get("num_used_gpu_blocks"))
        running += _as_int(b.get("num_requests_running"))
        waiting += _as_int(b.get("num_requests_waiting"))
        pq += _as_float(b.get("prefix_cache_queries_total"))
        ph += _as_float(b.get("prefix_cache_hits_total"))
        pt += _as_float(b.get("prompt_tokens_total"))
        gt += _as_float(b.get("generation_tokens_total"))
        pre += _as_float(b.get("num_preemptions_total"))
        if bs > 0:
            block_sizes.append(bs)

    distinct = sorted(set(block_sizes))
    if len(distinct) > 1:
        raise ValueError(
            f"instances disagree on block_size={distinct}; "
            "a cluster-wide KV capacity is undefined"
        )

    return {
        "num_instances": seen,
        "num_healthy": healthy,
        "block_size": distinct[0] if distinct else 0,
        "num_gpu_blocks": total,
        "num_used_gpu_blocks": used,
        "num_free_gpu_blocks": total - used,
        "kv_cache_usage_perc": (used / total) if total else 0.0,
        "num_requests_running": running,
        "num_requests_waiting": waiting,
        "prefix_cache_queries_total": pq,
        "prefix_cache_hits_total": ph,
        "prefix_cache_hit_rate": (ph / pq) if pq else 0.0,
        "prompt_tokens_total": pt,
        "generation_tokens_total": gt,
        "num_preemptions_total": pre,
    }


def is_ready(aggregate: Dict[str, Any]) -> bool:
    return bool(aggregate.get("block_size")) and bool(aggregate.get("num_gpu_blocks"))


def render_prometheus(aggregate: Dict[str, Any]) -> str:
    lines: List[str] = []

    def gauge(name: str, value: Any, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} gauge")
        lines.append(f'{name}{{instance="cluster"}} {float(value)!r}')

    def counter(name: str, value: Any, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        lines.append(f'{name}{{instance="cluster"}} {float(value)!r}')

    if is_ready(aggregate):
        labels = (
            f'block_size="{aggregate["block_size"]}",'
            f'num_gpu_blocks="{aggregate["num_gpu_blocks"]}"'
        )
        lines.append("# HELP vllm:cache_config_info KV cache geometry of the cluster")
        lines.append("# TYPE vllm:cache_config_info gauge")
        lines.append(f"vllm:cache_config_info{{{labels}}} 1.0")

    gauge("vllm:kv_cache_usage_perc", aggregate["kv_cache_usage_perc"],
          "Fraction of cluster KV blocks in use")
    gauge("vllm:num_requests_running", aggregate["num_requests_running"],
          "Requests currently decoding on the cluster")
    gauge("vllm:num_requests_waiting", aggregate["num_requests_waiting"],
          "Requests queued on the cluster")
    counter("vllm:prefix_cache_queries_total", aggregate["prefix_cache_queries_total"],
            "Cluster prefix-cache lookups")
    counter("vllm:prefix_cache_hits_total", aggregate["prefix_cache_hits_total"],
            "Cluster prefix-cache hits")
    counter("vllm:prompt_tokens_total", aggregate["prompt_tokens_total"],
            "Cluster prompt tokens processed")
    counter("vllm:generation_tokens_total", aggregate["generation_tokens_total"],
            "Cluster generation tokens emitted")
    counter("vllm:num_preemptions_total", aggregate["num_preemptions_total"],
            "Cluster preemptions")

    gauge("executor:num_instances", aggregate["num_instances"], "Instances configured")
    gauge("executor:num_healthy", aggregate["num_healthy"], "Instances responding")
    gauge("executor:num_used_gpu_blocks", aggregate["num_used_gpu_blocks"],
          "Cluster KV blocks in use")

    for instance, count in sorted(aggregate.get("programs_pinned", {}).items()):
        lines.append("# HELP executor:programs_pinned Programs pinned to one instance")
        lines.append("# TYPE executor:programs_pinned gauge")
        lines.append(f'executor:programs_pinned{{instance="{instance}"}} {int(count)}')

    return "\n".join(lines) + "\n"
