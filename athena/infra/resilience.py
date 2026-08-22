"""Resilience adapters for model, storage, and other external calls."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, TypeVar

from athena.infra.cache import CacheBackend, RedisCache
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse

logger = logging.getLogger(__name__)
T = TypeVar("T")


class CircuitBreakerError(Exception):
    """Raised when a dependency circuit is open."""


class AsyncCircuitBreaker:
    """Small three-state asynchronous circuit breaker."""

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
                    self._state = "half_open"
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
    if isinstance(exc, CircuitBreakerError):
        return False
    if isinstance(exc, (ValueError, TypeError, KeyError, PermissionError)):
        return False
    if isinstance(exc, (TimeoutError, ConnectionError, asyncio.TimeoutError)):
        return True
    return any(marker in str(exc).lower() for marker in _RETRYABLE_MARKERS)


@dataclass
class RetryPolicy:
    max_attempts: int = 3
    initial_seconds: float = 0.5
    max_seconds: float = 8.0
    jitter: float = 0.1


async def retry_async(
    func: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy | None = None,
    retryable: Callable[[BaseException], bool] = is_retryable,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    policy = policy or RetryPolicy()
    delay = policy.initial_seconds
    last_exc: BaseException | None = None
    for attempt in range(1, policy.max_attempts + 1):
        try:
            return await func()
        except BaseException as exc:  # noqa: BLE001
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
    assert last_exc is not None
    raise last_exc


def make_breaker(
    name: str, fail_max: int = 5, reset_timeout: float = 30.0
) -> AsyncCircuitBreaker:
    return AsyncCircuitBreaker(
        name=name, fail_max=fail_max, reset_timeout=reset_timeout
    )


def default_fault_diagnose_fallback(messages: Sequence[LLMMessage]) -> LLMResponse:
    del messages
    payload = {
        "thought": "LLM unavailable; use safe local fallback",
        "action": None,
        "action_input": {},
        "final_answer": (
            "The model is temporarily unavailable. Check the latest backend logs, "
            "recent changes, dependency health, and resource pressure, then retry."
        ),
    }
    return LLMResponse(
        content=json.dumps(payload),
        model="fallback-local-rules",
    )


class ResilientLLMClient:
    """LLM client decorator combining retry, circuit breaking, and fallback."""

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
        return await self._breaker.call_async(self._inner.complete, messages)

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        try:
            return await retry_async(
                lambda: self._guarded_call(messages), policy=self._policy
            )
        except Exception as exc:  # noqa: BLE001
            if self._fallback is not None:
                logger.warning("LLM call failed after resilience, degrading: %s", exc)
                return self._fallback(messages)
            raise


class RateLimitExceeded(Exception):
    """Raised when a fixed-window request quota is exceeded."""

    def __init__(self, scope: str, limit: int, retry_after_seconds: int = 60) -> None:
        super().__init__(f"rate limit exceeded for {scope} (limit={limit}/min)")
        self.scope = scope
        self.limit = limit
        self.retry_after_seconds = retry_after_seconds


class RateLimiter:
    """Simple global and tenant fixed-window limiter."""

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
    """Global, tenant, route, and optional model token buckets."""

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
