"""
📦 韧性基础设施：指数退避重试 + 熔断器 + 限流器 + 韧性 LLM 客户端
📍 架构位置：基础设施层，包裹所有外部依赖（大模型/向量库/K8s）的调用。
🎯 核心作用：
    - 指数退避重试：区分可重试（网络超时）与不可重试（参数错误）错误。
    - 熔断器：外部依赖失败率超阈值自动熔断，快速失败，避免雪崩。
    - 降级：大模型不可用时返回本地规则兜底答案。
    - 限流器：基于缓存的固定窗口计数，支持全局 + 单租户维度。
🔗 依赖：内置 AsyncCircuitBreaker（熔断）；infra.cache（限流计数）；被 cli.build_agent / api 中间件使用。
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import threading
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from athena.infra.cache import CacheBackend, RedisCache
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse

logger = logging.getLogger(__name__)

T = TypeVar("T")


class CircuitBreakerError(Exception):
    """熔断器已打开：快速失败，避免持续冲击已故障的依赖。"""


class AsyncCircuitBreaker:
    """
    轻量级异步熔断器（无 tornado 依赖）。

    三态：closed(正常) → 连续失败达 fail_max 次 → open(快速失败) →
    经过 reset_timeout 秒 → half_open(放行一次试探)，成功则回到 closed。
    """

    def __init__(
        self, name: str, fail_max: int = 5, reset_timeout: float = 30.0
    ) -> None:
        self.name = name
        self.fail_max = fail_max
        self.reset_timeout = reset_timeout
        self._failures = 0
        self._state = "closed"
        self._opened_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def state(self) -> str:
        return self._state

    async def call_async(
        self, func: Callable[..., Awaitable[T]], *args: Any, **kwargs: Any
    ) -> T:
        async with self._lock:
            if self._state == "open":
                if time.monotonic() - self._opened_at >= self.reset_timeout:
                    self._state = "half_open"  # 允许一次试探
                else:
                    raise CircuitBreakerError(f"circuit '{self.name}' is open")
        try:
            result = await func(*args, **kwargs)
        except BaseException:
            async with self._lock:
                self._failures += 1
                if self._state == "half_open" or self._failures >= self.fail_max:
                    self._state = "open"
                    self._opened_at = time.monotonic()
            raise
        async with self._lock:
            self._failures = 0
            self._state = "closed"
        return result


# 错误信息里出现这些标记通常意味着"可重试"（瞬时故障）
_RETRYABLE_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "temporarily",
    "rate limit",
    "too many requests",
    "502",
    "503",
    "504",
    "econnreset",
    "reset by peer",
    "unavailable",
)


def is_retryable(exc: BaseException) -> bool:
    """
    判断异常是否值得重试。

    不可重试：参数/类型错误（重试也没用）、熔断器已打开（应快速失败）。
    可重试：网络超时、连接失败、限流、5xx 等瞬时故障。
    """
    if isinstance(exc, CircuitBreakerError):
        return False  # 熔断已打开，重试只会继续撞墙
    if isinstance(exc, (ValueError, TypeError, KeyError, PermissionError)):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)


@dataclass
class RetryPolicy:
    """指数退避重试策略。"""

    max_attempts: int = 3
    initial_seconds: float = 0.5
    max_seconds: float = 8.0
    jitter: float = 0.1  # 抖动比例，避免"惊群"同时重试


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    retryable: Callable[[BaseException], bool] = is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """
    带指数退避 + 抖动的异步重试执行器。

    达到最大次数或遇到不可重试错误时，向上抛出最后一次异常。
    sleep 可注入，便于测试时替换为无等待实现。
    """
    policy = policy or RetryPolicy()
    delay = policy.initial_seconds
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await func()
        except BaseException as exc:  # noqa: BLE001 - 需要统一分类后再决定是否重试
            last_exc = exc
            if attempt >= policy.max_attempts or not retryable(exc):
                raise
            wait = min(delay, policy.max_seconds)
            wait += random.uniform(0, wait * policy.jitter)
            logger.warning(
                "retry attempt=%d/%d after %.2fs due to: %s",
                attempt,
                policy.max_attempts,
                wait,
                exc,
            )
            await sleep(wait)
            delay *= 2
    assert last_exc is not None  # 逻辑上不可达
    raise last_exc


def make_breaker(
    name: str, fail_max: int = 5, reset_timeout: float = 30.0
) -> AsyncCircuitBreaker:
    """创建一个命名熔断器：连续失败达 fail_max 次后打开，reset_timeout 秒后半开试探。"""
    return AsyncCircuitBreaker(
        name=name, fail_max=fail_max, reset_timeout=reset_timeout
    )


def default_fault_diagnose_fallback(messages: Sequence[LLMMessage]) -> LLMResponse:
    """
    大模型不可用时的本地规则兜底：返回合法的 ReAct JSON，给出通用排障建议。

    返回内容必须是 Agent 能解析的 ReAct 决策格式，否则会二次失败。
    """
    advice = (
        "大模型暂时不可用，已降级为本地规则排障建议：\\n"
        "1) 检查目标服务进程与端口是否存活；\\n"
        "2) 查看最近变更/发布记录，优先回滚可疑变更；\\n"
        "3) 检查依赖（数据库/缓存/下游API）连通性与错误日志；\\n"
        "4) 关注 CPU/内存/磁盘/网络资源水位是否打满；\\n"
        "5) 如未恢复，请升级人工介入。"
    )
    payload = {
        "thought": "LLM unavailable, degrade to local rule-based advice",
        "action": None,
        "action_input": {},
        "final_answer": advice,
    }
    import json

    return LLMResponse(
        content=json.dumps(payload, ensure_ascii=False), model="fallback-local-rules"
    )


class ResilientLLMClient:
    """
    韧性 LLM 客户端装饰器：在原始客户端外叠加 重试 + 熔断 + 降级。

    满足 LLMClient 协议（有 complete 方法），可无缝替换原始客户端。
    """

    def __init__(
        self,
        inner: LLMClient,
        retry_policy: RetryPolicy | None = None,
        breaker: AsyncCircuitBreaker | None = None,
        fallback: Callable[[Sequence[LLMMessage]], LLMResponse] | None = None,
    ) -> None:
        self._inner = inner
        self._policy = retry_policy or RetryPolicy()
        self._breaker = breaker or make_breaker("llm")
        self._fallback = fallback

    async def _guarded_call(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        # 熔断器包裹真实调用：失败计数由熔断器维护
        return await self._breaker.call_async(self._inner.complete, messages)

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        try:
            return await retry_async(
                lambda: self._guarded_call(messages), policy=self._policy
            )
        except Exception as exc:  # noqa: BLE001 - 最外层降级出口
            if self._fallback is not None:
                logger.warning("LLM call failed after resilience, degrading: %s", exc)
                return self._fallback(messages)
            raise


class RateLimitExceeded(Exception):
    """限流触发：请求超过窗口配额。"""

    def __init__(self, scope: str, limit: int, retry_after_seconds: int = 60) -> None:
        super().__init__(f"rate limit exceeded for {scope} (limit={limit}/min)")
        self.scope = scope
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


class RateLimiter:
    """
    基于缓存的固定窗口限流器（1 分钟窗口）。

    同时约束全局与单租户维度；底层用 cache.incr 原子自增，Redis/内存后端通用。
    """

    def __init__(
        self,
        cache: CacheBackend,
        global_per_minute: int = 600,
        per_tenant_per_minute: int = 120,
    ) -> None:
        self._cache = cache
        self._global = global_per_minute
        self._per_tenant = per_tenant_per_minute

    def check(self, tenant_id: str) -> None:
        """超限时抛 RateLimitExceeded；未超限则记一次计数。"""
        window = int(time.time() // 60)
        global_count = self._cache.incr(f"rl:global:{window}", ttl_seconds=60)
        if global_count > self._global:
            raise RateLimitExceeded("global", self._global)
        tenant_count = self._cache.incr(
            f"rl:tenant:{tenant_id}:{window}", ttl_seconds=60
        )
        if tenant_count > self._per_tenant:
            raise RateLimitExceeded(f"tenant:{tenant_id}", self._per_tenant)


_TOKEN_BUCKET_LUA = """
local now = tonumber(ARGV[1])
local cost = tonumber(ARGV[2])
local minimum_retry = 0
for index = 1, #KEYS do
  local capacity = tonumber(ARGV[2 + index * 2 - 1])
  local refill_per_ms = tonumber(ARGV[2 + index * 2])
  local values = redis.call('HMGET', KEYS[index], 'tokens', 'timestamp')
  local tokens = tonumber(values[1]) or capacity
  local timestamp = tonumber(values[2]) or now
  local elapsed = math.max(0, now - timestamp)
  tokens = math.min(capacity, tokens + elapsed * refill_per_ms)
  if tokens < cost then
    local retry = math.ceil((cost - tokens) / refill_per_ms)
    if retry > minimum_retry then minimum_retry = retry end
  end
end
if minimum_retry > 0 then return {0, minimum_retry} end
for index = 1, #KEYS do
  local capacity = tonumber(ARGV[2 + index * 2 - 1])
  local refill_per_ms = tonumber(ARGV[2 + index * 2])
  local values = redis.call('HMGET', KEYS[index], 'tokens', 'timestamp')
  local tokens = tonumber(values[1]) or capacity
  local timestamp = tonumber(values[2]) or now
  local elapsed = math.max(0, now - timestamp)
  tokens = math.min(capacity, tokens + elapsed * refill_per_ms) - cost
  redis.call('HMSET', KEYS[index], 'tokens', tokens, 'timestamp', now)
  redis.call('PEXPIRE', KEYS[index], math.ceil(capacity / refill_per_ms * 2))
end
return {1, 0}
"""


class HierarchicalRateLimiter:
    """Atomic global/tenant/route/model token buckets with Redis Lua and memory fallback."""

    def __init__(
        self,
        cache: CacheBackend,
        *,
        global_per_minute: int,
        per_tenant_per_minute: int,
        per_route_per_minute: int,
        per_model_per_minute: int | None = None,
        burst_multiplier: float = 1.0,
    ) -> None:
        if burst_multiplier <= 0:
            raise ValueError("burst_multiplier must be positive")
        capacities = [
            max(1, int(global_per_minute * burst_multiplier)),
            max(1, int(per_tenant_per_minute * burst_multiplier)),
            max(1, int(per_route_per_minute * burst_multiplier)),
        ]
        if per_model_per_minute is not None:
            capacities.append(max(1, int(per_model_per_minute * burst_multiplier)))
        self._cache = cache
        self._capacities = tuple(capacities)
        self._rates = tuple(capacity / 60_000 for capacity in self._capacities)
        self._memory: dict[str, tuple[float, float]] = {}
        self._lock = threading.RLock()

    async def check(
        self, tenant_id: str, route: str, *, cost: int = 1, model: str | None = None
    ) -> None:
        if cost <= 0:
            raise ValueError("rate limit cost must be positive")
        if isinstance(self._cache, RedisCache):
            allowed, retry_ms = await asyncio.to_thread(
                self._redis_check, tenant_id, route, cost, model
            )
        else:
            allowed, retry_ms = self._memory_check(tenant_id, route, cost, model)
        if not allowed:
            scope = f"tenant:{tenant_id}:route:{route}"
            if model:
                scope = f"{scope}:model:{model}"
            raise RateLimitExceeded(
                scope,
                min(self._capacities),
                retry_after_seconds=max(1, int((retry_ms + 999) / 1000)),
            )

    def _active_limits(
        self, model: str | None
    ) -> tuple[tuple[int, ...], tuple[float, ...]]:
        if model or len(self._capacities) <= 3:
            return self._capacities, self._rates
        return self._capacities[:3], self._rates[:3]

    def _redis_check(
        self, tenant_id: str, route: str, cost: int, model: str | None
    ) -> tuple[bool, int]:
        keys = [
            self._cache._k("tb:global"),
            self._cache._k(f"tb:tenant:{tenant_id}"),
            self._cache._k(f"tb:route:{tenant_id}:{route}"),
        ]
        if model and len(self._capacities) > 3:
            keys.append(self._cache._k(f"tb:model:{tenant_id}:{model}"))
        capacities, rates = self._active_limits(model)
        args: list[float | int] = [int(time.time() * 1000), cost]
        for capacity, rate in zip(capacities, rates, strict=True):
            args.extend([capacity, rate])
        result = self._cache.client.eval(_TOKEN_BUCKET_LUA, len(keys), *keys, *args)
        return bool(int(result[0])), int(result[1])

    def _memory_check(
        self, tenant_id: str, route: str, cost: int, model: str | None
    ) -> tuple[bool, int]:
        now = time.monotonic() * 1000
        keys = ["global", f"tenant:{tenant_id}", f"route:{tenant_id}:{route}"]
        if model and len(self._capacities) > 3:
            keys.append(f"model:{tenant_id}:{model}")
        capacities, rates = self._active_limits(model)
        with self._lock:
            candidates: list[tuple[str, float, float, int, float]] = []
            retry_ms = 0
            for key, capacity, rate in zip(keys, capacities, rates, strict=True):
                previous_tokens, previous_at = self._memory.get(
                    key, (float(capacity), now)
                )
                tokens = min(
                    capacity, previous_tokens + max(0, now - previous_at) * rate
                )
                candidates.append((key, tokens, now, capacity, rate))
                if tokens < cost:
                    retry_ms = max(retry_ms, int((cost - tokens) / rate) + 1)
            if retry_ms:
                return False, retry_ms
            for key, tokens, timestamp, _, _ in candidates:
                self._memory[key] = (tokens - cost, timestamp)
            return True, 0
