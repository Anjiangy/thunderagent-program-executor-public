"""Logic tests that need no FastAPI: parsing, aggregation, and the pin table."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from executor.cluster_metrics import aggregate_backends, is_ready, render_prometheus
from executor.programs import ProgramTable
from executor.vllm_metrics import parse_metrics, unhealthy

SAMPLE = """# HELP vllm:cache_config_info Cache configuration
# TYPE vllm:cache_config_info gauge
vllm:cache_config_info{block_size="128",engine="0",num_gpu_blocks="892"} 1.0
# HELP vllm:kv_cache_usage_perc KV-cache usage
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="m"} 0.25
vllm:num_requests_running{engine="0",model_name="m"} 3.0
vllm:num_requests_waiting{engine="0",model_name="m"} 1.0
vllm:prefix_cache_queries_total{engine="0"} 100.0
vllm:prefix_cache_hits_total{engine="0"} 80.0
vllm:num_preemptions_total{engine="0"} 2.0
vllm:prompt_tokens_total{engine="0"} 500.0
vllm:generation_tokens_total{engine="0"} 100.0
"""


def test_parse_scalar_fields():
    info = parse_metrics(SAMPLE)
    assert info["healthy"] is True
    assert info["num_gpu_blocks"] == 892
    assert info["block_size"] == 128
    assert abs(info["kv_cache_usage_perc"] - 0.25) < 1e-9
    assert info["num_requests_running"] == 3
    assert info["num_requests_waiting"] == 1
    assert info["prefix_cache_queries_total"] == 100
    assert info["prefix_cache_hits_total"] == 80
    assert info["num_preemptions_total"] == 2
    assert info["prompt_tokens_total"] == 500
    assert info["generation_tokens_total"] == 100


def test_parse_garbage_is_unhealthy():
    info = parse_metrics("<html>404</html>")
    assert info["healthy"] is False
    assert "no cache_config_info" in info["error"]
    assert info["num_gpu_blocks"] == 0


def test_aggregate_sums_blocks_and_weights_usage():
    a = dict(parse_metrics(SAMPLE))
    b = dict(parse_metrics(SAMPLE.replace('num_gpu_blocks="892"', 'num_gpu_blocks="1000"')))
    b["kv_cache_usage_perc"] = 0.75
    b["num_used_gpu_blocks"] = 750
    b["num_requests_running"] = 1
    b["num_requests_waiting"] = 0

    aggregate = aggregate_backends([a, b])
    assert aggregate["num_gpu_blocks"] == 1892
    assert aggregate["block_size"] == 128
    expected = (0.25 * 892 + 0.75 * 1000) / 1892
    assert abs(aggregate["kv_cache_usage_perc"] - expected) < 1e-9
    assert aggregate["num_requests_running"] == 4


def test_aggregate_skips_unhealthy():
    a = dict(parse_metrics(SAMPLE))
    aggregate = aggregate_backends([a, unhealthy("boom")])
    assert aggregate["num_gpu_blocks"] == 892
    assert is_ready(aggregate) is True


def test_render_prometheus_roundtrip():
    text = render_prometheus(aggregate_backends([parse_metrics(SAMPLE)]))
    assert 'vllm:cache_config_info{block_size="128",num_gpu_blocks="892"}' in text
    assert "vllm:kv_cache_usage_perc{" in text
    assert "vllm:num_requests_running{" in text
    assert " 3.0" in text
    # The rendered text must parse back to the same numbers.
    back = parse_metrics(text)
    assert back["num_gpu_blocks"] == 892
    assert back["num_requests_running"] == 3


def test_not_ready_when_capacity_zero():
    assert is_ready(aggregate_backends([unhealthy("x")])) is False


def test_pin_table_assigns_and_keeps_affinity():
    table = ProgramTable()
    live = {
        "a": {"healthy": True, "num_requests_running": 0, "kv_cache_usage_perc": 0.1},
        "b": {"healthy": True, "num_requests_running": 5, "kv_cache_usage_perc": 0.9},
    }
    table.touch("p1")
    home = table.assign("p1", live)
    assert home == "a"
    # Second call keeps the pin even though load changed.
    live["a"]["num_requests_running"] = 9
    assert table.assign("p1", live) == "a"


def test_pin_table_failover_when_home_dies():
    table = ProgramTable()
    live = {
        "a": {"healthy": True, "num_requests_running": 0, "kv_cache_usage_perc": 0.1},
        "b": {"healthy": True, "num_requests_running": 1, "kv_cache_usage_perc": 0.1},
    }
    table.touch("p1")
    assert table.assign("p1", live) == "a"
    live["a"]["healthy"] = False
    assert table.assign("p1", live) == "b"
    assert table.get("p1").last_move["mode"] == "failover"


def test_migration_lifecycle():
    table = ProgramTable()
    live = {n: {"healthy": True, "num_requests_running": 0, "kv_cache_usage_perc": 0.0}
            for n in ("a", "b")}
    table.touch("p1")
    table.assign("p1", live)
    entry = table.begin_migration("p1", "b", "recompute")
    assert entry.migrating is True
    try:
        table.begin_migration("p1", "b", "recompute")
        raise AssertionError("expected RuntimeError for double migration")
    except RuntimeError:
        pass
    table.finish_migration("p1", ok=True, waited_ms=12)
    assert table.get("p1").instance == "b"
    assert table.get("p1").migrations == 1
    assert table.get("p1").migrating is False


def test_migration_rejects_same_target():
    table = ProgramTable()
    live = {"a": {"healthy": True, "num_requests_running": 0, "kv_cache_usage_perc": 0.0}}
    table.touch("p1")
    table.assign("p1", live)
    try:
        table.begin_migration("p1", "a", "recompute")
        raise AssertionError("expected ValueError for same target")
    except ValueError:
        pass


# -- M4: prompt memory and migration candidates -------------------------


def test_record_prompt_keeps_the_latest_body():
    table = ProgramTable()
    table.touch("p1")
    table.record_prompt("p1", "/v1/chat/completions", {"messages": [{"content": "y" * 3600}]})
    big = table.get("p1").prompt_tokens
    assert big > 0

    # A later, shorter body still wins: it is the context the next turn extends.
    # Keeping the largest one would replay bytes the program no longer sends.
    table.record_prompt("p1", "/v1/chat/completions", {"messages": [{"content": "x" * 9}]})
    assert table.get("p1").prompt_tokens < big
    assert table.get("p1").prompt_body["messages"][0]["content"] == "x" * 9
    assert table.get("p1").to_dict()["warm_ready"] is True


def test_migration_candidates_biggest_first_and_skips_busy():
    table = ProgramTable()
    live = {n: {"healthy": True, "num_requests_running": 0, "kv_cache_usage_perc": 0.0}
            for n in ("a", "b")}
    only_a = {"a": live["a"]}            # pin everything to "a" on purpose
    for pid, size in (("p-small", 100), ("p-big", 9000), ("p-mid", 3000)):
        table.touch(pid)
        table.assign(pid, only_a)
        table.record_prompt(pid, "/v1/chat/completions",
                            {"messages": [{"content": "z" * int(size * 3.6)}]})
    table.begin_request("p-mid")         # busy programs are not movable
    assert table.migration_candidates("a") == ["p-big", "p-small"]
    table.end_request("p-mid")
    assert table.migration_candidates("a") == ["p-big", "p-mid", "p-small"]


def test_candidates_skip_programs_without_recorded_prompt():
    table = ProgramTable()
    live = {n: {"healthy": True, "num_requests_running": 0, "kv_cache_usage_perc": 0.0}
            for n in ("a", "b")}
    table.touch("p1")
    table.assign("p1", live)
    assert table.migration_candidates("a") == []


def test_record_warmed_and_counts():
    table = ProgramTable()
    table.touch("p1")
    table.record_prompt("p1", "/v1/chat/completions", {"messages": [{"content": "q" * 360}]})
    table.record_warmed("p1", 123)
    assert table.get("p1").warmed_tokens == 123
    counts = table.counts()
    assert counts["warm_ready"] == 1
    assert counts["programs"] == 1


def test_kvp_blob_is_single_use() -> None:
    table = ProgramTable()
    table.touch("p")
    blob = {"remote_block_ids": [[1, 2]], "remote_host": "10.0.0.1", "remote_port": 14579}
    assert table.set_kvp("p", blob) is True
    assert table.get("p").to_dict()["kvp_ready"] is True
    taken = table.take_kvp("p")
    assert taken == blob
    assert taken is not blob
    assert table.get("p").to_dict()["kvp_ready"] is False
    assert table.take_kvp("p") is None


def test_kvp_for_unknown_program_is_refused() -> None:
    table = ProgramTable()
    assert table.set_kvp("ghost", {"remote_block_ids": [[1]]}) is False
    assert table.take_kvp("ghost") is None
