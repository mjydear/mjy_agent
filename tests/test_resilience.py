"""阶段3 高可用测试：退避重试、熔断、降级、限流、L0-L4 异常分级。"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from athena.exceptions import AgentError, ErrorCode, LLMError
from athena.infra.cache import InMemoryCache
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.infra.resilience import (
    CircuitBreakerError,
    HierarchicalRateLimiter,
    RateLimiter,
    RateLimitExceeded,
    ResilientLLMClient,
    RetryPolicy,
    default_fault_diagnose_fallback,
    is_retryable,
    make_breaker,
    retry_async,
)
from athena.observability.incident import (
    Severity,
    Strategy,
    IncidentManager,
    classify_incident,
)


async def _no_sleep(_seconds: float) -> None:
    return None


@pytest.mark.asyncio
async def test_retry_recovers_after_transient_failures() -> None:
    calls = {"n": 0}

    async def flaky() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("connection reset")
        return "ok"

    result = await retry_async(
        flaky, policy=RetryPolicy(max_attempts=5, initial_seconds=0.0), sleep=_no_sleep
    )
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_does_not_retry_non_retryable() -> None:
    calls = {"n": 0}

    async def bad_param() -> str:
        calls["n"] += 1
        raise ValueError("invalid argument")

    with pytest.raises(ValueError):
        await retry_async(
            bad_param, policy=RetryPolicy(max_attempts=5), sleep=_no_sleep
        )
    assert calls["n"] == 1  # 参数错误不重试


def test_is_retryable_classification() -> None:
    assert is_retryable(TimeoutError("timed out"))
    assert is_retryable(ConnectionError("connection reset"))
    assert is_retryable(RuntimeError("503 service unavailable"))
    assert not is_retryable(ValueError("bad"))
    assert not is_retryable(CircuitBreakerError("open"))


class AlwaysFailLLM(LLMClient):
    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        raise LLMError(ErrorCode.LLM_CALL_FAILED, "connection timeout")


@pytest.mark.asyncio
async def test_resilient_llm_falls_back_when_unavailable() -> None:
    client = ResilientLLMClient(
        AlwaysFailLLM(),
        retry_policy=RetryPolicy(max_attempts=2, initial_seconds=0.0),
        breaker=make_breaker("test-llm", fail_max=10),
        fallback=default_fault_diagnose_fallback,
    )
    resp = await client.complete([LLMMessage(role="user", content="服务挂了")])
    assert resp.model == "fallback-local-rules"
    assert "本地规则排障" in resp.content


@pytest.mark.asyncio
async def test_resilient_llm_reraises_without_fallback() -> None:
    client = ResilientLLMClient(
        AlwaysFailLLM(),
        retry_policy=RetryPolicy(max_attempts=1, initial_seconds=0.0),
        breaker=make_breaker("test-llm-2", fail_max=10),
        fallback=None,
    )
    with pytest.raises(LLMError):
        await client.complete([LLMMessage(role="user", content="x")])


def test_rate_limiter_global_and_tenant() -> None:
    cache = InMemoryCache(namespace="rltest")
    limiter = RateLimiter(cache, global_per_minute=100, per_tenant_per_minute=3)
    for _ in range(3):
        limiter.check("tenant-a")
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.check("tenant-a")
    assert "tenant" in exc.value.scope


def test_rate_limiter_global_cap() -> None:
    cache = InMemoryCache(namespace="rlg")
    limiter = RateLimiter(cache, global_per_minute=2, per_tenant_per_minute=100)
    limiter.check("a")
    limiter.check("b")
    with pytest.raises(RateLimitExceeded) as exc:
        limiter.check("c")
    assert exc.value.scope == "global"


@pytest.mark.asyncio
async def test_hierarchical_rate_limiter_enforces_atomic_route_bucket() -> None:
    limiter = HierarchicalRateLimiter(
        InMemoryCache(namespace="token-bucket"),
        global_per_minute=10,
        per_tenant_per_minute=10,
        per_route_per_minute=2,
    )
    await limiter.check("tenant-a", "/api/ops/tasks")
    await limiter.check("tenant-a", "/api/ops/tasks")
    with pytest.raises(RateLimitExceeded) as exc:
        await limiter.check("tenant-a", "/api/ops/tasks")
    assert exc.value.retry_after_seconds >= 1


def test_incident_classification_levels() -> None:
    assert classify_incident(PermissionError("sandbox breach")).severity == Severity.L4
    assert classify_incident(ValueError("bad")).strategy == Strategy.REJECT
    assert (
        classify_incident(LLMError(ErrorCode.LLM_CALL_FAILED, "boom")).severity
        == Severity.L2
    )
    assert (
        classify_incident(AgentError(ErrorCode.AGENT_EXECUTION_FAILED, "x")).severity
        == Severity.L3
    )
    assert classify_incident(TimeoutError("timed out")).severity == Severity.L1


def test_incident_manager_records_and_alerts() -> None:
    alerts: list[str] = []
    manager = IncidentManager(alert_sink=lambda inc: alerts.append(inc.error_code))
    manager.record(TimeoutError("timed out"))  # L1, no alert
    manager.record(PermissionError("breach"))  # L4, alert
    stats = manager.stats()
    assert stats["L1"] == 1
    assert stats["L4"] == 1
    assert len(alerts) == 1  # 仅 L3+ 触发告警
