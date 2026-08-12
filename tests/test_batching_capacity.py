"""Bounded micro-batch, backpressure and weighted rate-limit tests."""

from __future__ import annotations

import asyncio

import pytest

from athena.infra.batching import BoundedMicroBatcher, MicroBatchSettings
from athena.infra.cache import InMemoryCache
from athena.infra.resilience import HierarchicalRateLimiter, RateLimitExceeded


@pytest.mark.asyncio
async def test_micro_batcher_splits_batches_and_isolates_partial_failure() -> None:
    seen: list[tuple[int, ...]] = []

    async def handler(batch):
        values = tuple(batch)
        seen.append(values)
        if values == (3, 4):
            raise RuntimeError("downstream unavailable")
        return [item * 10 for item in values]

    batcher = BoundedMicroBatcher[int, int](
        MicroBatchSettings(max_batch_size=2, max_wait_ms=5, max_concurrency=1),
        handler,
    )

    result = await batcher.run_once((1, 2, 3, 4, 5))

    assert seen == [(1, 2), (3, 4), (5,)]
    assert result.attempted == 5
    assert result.succeeded == 3
    assert result.failed == 2
    assert [item.value for item in result.results if item.ok] == [10, 20, 50]


@pytest.mark.asyncio
async def test_micro_batcher_collects_until_size_or_wait() -> None:
    queue: asyncio.Queue[int] = asyncio.Queue()
    await queue.put(1)
    await queue.put(2)
    await queue.put(3)

    async def handler(batch):
        return list(batch)

    batcher = BoundedMicroBatcher[int, int](
        MicroBatchSettings(max_batch_size=2, max_wait_ms=100, max_concurrency=1),
        handler,
    )

    result = await batcher.collect_and_run(queue)

    assert result.attempted == 2
    assert queue.qsize() == 1


@pytest.mark.asyncio
async def test_hierarchical_rate_limiter_supports_weighted_model_bucket() -> None:
    limiter = HierarchicalRateLimiter(
        InMemoryCache(namespace="weighted-token-bucket"),
        global_per_minute=100,
        per_tenant_per_minute=100,
        per_route_per_minute=100,
        per_model_per_minute=5,
    )

    await limiter.check("tenant-a", "/api/chat", cost=3, model="gpt-large")

    with pytest.raises(RateLimitExceeded) as exc:
        await limiter.check("tenant-a", "/api/chat", cost=3, model="gpt-large")

    assert "model:gpt-large" in exc.value.scope
