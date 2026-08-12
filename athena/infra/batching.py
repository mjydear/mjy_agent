"""Bounded micro-batch utilities for durable background pipelines."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class MicroBatchSettings:
    max_batch_size: int
    max_wait_ms: int
    max_concurrency: int

    def __post_init__(self) -> None:
        if self.max_batch_size <= 0:
            raise ValueError("max_batch_size must be positive")
        if self.max_wait_ms < 0:
            raise ValueError("max_wait_ms must be non-negative")
        if self.max_concurrency <= 0:
            raise ValueError("max_concurrency must be positive")


@dataclass(frozen=True)
class BatchItemResult(Generic[R]):
    ok: bool
    value: R | None = None
    error: str | None = None


@dataclass(frozen=True)
class BatchRunResult(Generic[R]):
    attempted: int
    succeeded: int
    failed: int
    results: tuple[BatchItemResult[R], ...]


class BoundedMicroBatcher(Generic[T, R]):
    """Flush bounded batches with semaphore-based downstream isolation."""

    def __init__(
        self,
        settings: MicroBatchSettings,
        handler: Callable[[Sequence[T]], Awaitable[Sequence[R]]],
    ) -> None:
        self._settings = settings
        self._handler = handler
        self._semaphore = asyncio.Semaphore(settings.max_concurrency)

    async def run_once(self, items: Sequence[T]) -> BatchRunResult[R]:
        if not items:
            return BatchRunResult(attempted=0, succeeded=0, failed=0, results=())
        chunks = tuple(
            tuple(items[index : index + self._settings.max_batch_size])
            for index in range(0, len(items), self._settings.max_batch_size)
        )
        chunk_results = await asyncio.gather(
            *(self._run_chunk(chunk) for chunk in chunks)
        )
        flat = tuple(result for chunk in chunk_results for result in chunk)
        succeeded = sum(1 for result in flat if result.ok)
        return BatchRunResult(
            attempted=len(items),
            succeeded=succeeded,
            failed=len(items) - succeeded,
            results=flat,
        )

    async def collect_and_run(self, queue: asyncio.Queue[T]) -> BatchRunResult[R]:
        """Collect one bounded batch from a queue and flush it.

        The first item waits indefinitely because the caller already decided to
        run this tick. Additional items are gathered until max size or max wait.
        """

        first = await queue.get()
        items: list[T] = [first]
        deadline = asyncio.get_running_loop().time() + (
            self._settings.max_wait_ms / 1000
        )
        while len(items) < self._settings.max_batch_size:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                items.append(await asyncio.wait_for(queue.get(), timeout=remaining))
            except TimeoutError:
                break
        return await self.run_once(items)

    async def _run_chunk(self, chunk: Sequence[T]) -> tuple[BatchItemResult[R], ...]:
        async with self._semaphore:
            try:
                values = tuple(await self._handler(chunk))
            except Exception as exc:  # noqa: BLE001 - isolate batch failure
                return tuple(
                    BatchItemResult(ok=False, error=str(exc)[:500]) for _ in chunk
                )
            if len(values) != len(chunk):
                return tuple(
                    BatchItemResult(ok=False, error="BATCH_RESULT_COUNT_MISMATCH")
                    for _ in chunk
                )
            return tuple(BatchItemResult(ok=True, value=value) for value in values)
