"""Parse the metrics vLLM 0.23 actually exposes, into one flat dict per instance.

Only the fields the capacity model needs are extracted, plus the prefix-cache
counters (which the cluster view *can* provide here, unlike the Llumnix path,
because we scrape real vLLM instances).
"""

from __future__ import annotations

import re
from typing import Any, Dict

_CFG = re.compile(r"^vllm:cache_config_info\{([^}]*)\}\s+([0-9.eE+\-]+)\s*$", re.M)
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="([^"]*)"')

#: metric name -> key in the returned dict
_SCALARS = {
    "vllm:kv_cache_usage_perc": "kv_cache_usage_perc",
    "vllm:num_requests_running": "num_requests_running",
    "vllm:num_requests_waiting": "num_requests_waiting",
    "vllm:prefix_cache_queries_total": "prefix_cache_queries_total",
    "vllm:prefix_cache_hits_total": "prefix_cache_hits_total",
    "vllm:prompt_tokens_total": "prompt_tokens_total",
    "vllm:generation_tokens_total": "generation_tokens_total",
    "vllm:num_preemptions_total": "num_preemptions_total",
}


def _sum_metric(text: str, name: str) -> float:
    """Sum every label combination of one metric (we only ever have one engine)."""
    pat = re.compile(rf"^{re.escape(name)}\{{[^}}]*\}}\s+([0-9.eE+\-]+)\s*$", re.M)
    values = [float(v) for v in pat.findall(text)]
    if not values:
        pat = re.compile(rf"^{re.escape(name)}\s+([0-9.eE+\-]+)\s*$", re.M)
        values = [float(v) for v in pat.findall(text)]
    return sum(values) if values else 0.0


def parse_metrics(text: str) -> Dict[str, Any]:
    """Turn one instance's Prometheus text into a flat dict. Never raises."""
    out: Dict[str, Any] = {
        "healthy": True,
        "block_size": 0,
        "num_gpu_blocks": 0,
        "enable_prefix_caching": False,
    }

    cfg = _CFG.search(text)
    if cfg:
        labels = dict(_LABEL.findall(cfg.group(1)))
        try:
            out["block_size"] = int(labels.get("block_size") or 0)
            out["num_gpu_blocks"] = int(labels.get("num_gpu_blocks") or 0)
        except (TypeError, ValueError):
            pass
        out["enable_prefix_caching"] = labels.get("enable_prefix_caching") == "True"

    if out["num_gpu_blocks"] <= 0:
        # No KV geometry means the text is not a vLLM metrics page at all
        # (404 page, HTML error, or an engine that has not started serving).
        return unhealthy(f"no cache_config_info with num_gpu_blocks in {len(text)} bytes")

    for metric, key in _SCALARS.items():
        out[key] = _sum_metric(text, metric)

    total = out["num_gpu_blocks"]
    usage = float(out.get("kv_cache_usage_perc") or 0.0)
    out["usage_clamped"] = min(max(usage, 0.0), 1.0)
    # Derive used blocks so the cluster view can weight by capacity, not average.
    out["num_used_gpu_blocks"] = int(round(out["usage_clamped"] * total)) if total else 0
    return out


def unhealthy(reason: str) -> Dict[str, Any]:
    return {
        "healthy": False,
        "error": reason,
        "block_size": 0,
        "num_gpu_blocks": 0,
        "num_used_gpu_blocks": 0,
        "kv_cache_usage_perc": 0.0,
        "num_requests_running": 0,
        "num_requests_waiting": 0,
        "prefix_cache_queries_total": 0.0,
        "prefix_cache_hits_total": 0.0,
        "prompt_tokens_total": 0.0,
        "generation_tokens_total": 0.0,
        "num_preemptions_total": 0.0,
    }
