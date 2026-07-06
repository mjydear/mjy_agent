"""任务/指标/评测报告持久化存储测试。"""

from __future__ import annotations

from athena.api.schemas import BenchmarkRunResponse, StepTrace
from athena.api.services import TaskRecord
from athena.api.task_store import BenchmarkStore, MetricsStore, TaskStore
from athena.infra.cache import InMemoryCache


def _cache() -> InMemoryCache:
    return InMemoryCache(namespace="test")


def test_task_store_roundtrip_with_steps() -> None:
    store = TaskStore(_cache(), ttl_seconds=60)
    record = TaskRecord(
        task_id="chat-1",
        status="success",
        answer="done",
        steps=[StepTrace(step_index=1, event_type="step", content="plan")],
    )
    store.save(record)
    loaded = store.get("chat-1")
    assert loaded is not None
    assert loaded.status == "success"
    assert loaded.answer == "done"
    assert len(loaded.steps) == 1
    assert loaded.steps[0].content == "plan"


def test_task_store_missing_returns_none() -> None:
    assert TaskStore(_cache()).get("missing") is None


def test_task_store_shared_cache_survives_new_instance() -> None:
    cache = _cache()
    TaskStore(cache).save(TaskRecord(task_id="t1", status="running"))
    # 新实例复用同一 cache（模拟另一副本/重启后重连 Redis）
    assert TaskStore(cache).get("t1") is not None


def test_metrics_store_error_distribution() -> None:
    store = MetricsStore(_cache())
    store.incr_error("ValueError")
    store.incr_error("ValueError")
    store.incr_error("KeyError")
    dist = store.error_distribution()
    assert dist == {"ValueError": 2, "KeyError": 1}


def test_metrics_store_token_usage() -> None:
    store = MetricsStore(_cache())
    assert store.token_usage() == 0
    store.add_tokens(100)
    store.add_tokens(50)
    assert store.token_usage() == 150


def test_benchmark_store_roundtrip() -> None:
    store = BenchmarkStore(_cache())
    store.save(
        BenchmarkRunResponse(run_id="bench-1", status="success", report="# R")
    )
    loaded = store.get("bench-1")
    assert loaded is not None
    assert loaded.report == "# R"
    assert store.get("nope") is None
