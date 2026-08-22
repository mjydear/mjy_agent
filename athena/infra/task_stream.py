"""Redis Streams transport for durable Runtime event delivery."""

from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TaskStreamMessage:
    message_id: str
    task_id: str
    tenant_id: str
    traceparent: str | None
    event_type: str | None = None
    deliveries: int = 1


class TaskStream(Protocol):
    async def publish(
        self,
        task_id: str,
        tenant_id: str,
        traceparent: str | None,
        event_type: str | None = None,
    ) -> str: ...

    async def consume(
        self, consumer: str, *, count: int, block_ms: int
    ) -> tuple[TaskStreamMessage, ...]: ...

    async def reclaim(
        self, consumer: str, *, min_idle_ms: int, count: int
    ) -> tuple[TaskStreamMessage, ...]: ...

    async def ack(self, message_id: str) -> None: ...

    async def dead_letter(self, message: TaskStreamMessage, error: str) -> None: ...

    async def close(self) -> None: ...


class InMemoryTaskStream:
    """Small async transport used by unit tests and explicit demo fallback."""

    def __init__(self) -> None:
        self._pending: deque[TaskStreamMessage] = deque()
        self._inflight: dict[str, TaskStreamMessage] = {}
        self._dead_letters: list[tuple[TaskStreamMessage, str]] = []
        self._sequence = 0
        self._condition = asyncio.Condition()

    async def publish(
        self,
        task_id: str,
        tenant_id: str,
        traceparent: str | None,
        event_type: str | None = None,
    ) -> str:
        async with self._condition:
            self._sequence += 1
            message = TaskStreamMessage(
                message_id=f"memory-{self._sequence}",
                task_id=task_id,
                tenant_id=tenant_id,
                traceparent=traceparent,
                event_type=event_type,
            )
            self._pending.append(message)
            self._condition.notify_all()
            return message.message_id

    async def consume(
        self, consumer: str, *, count: int, block_ms: int
    ) -> tuple[TaskStreamMessage, ...]:
        del consumer
        async with self._condition:
            if not self._pending and block_ms:
                try:
                    await asyncio.wait_for(
                        self._condition.wait(), timeout=block_ms / 1000
                    )
                except TimeoutError:
                    return ()
            messages = tuple(
                self._pending.popleft() for _ in range(min(count, len(self._pending)))
            )
            self._inflight.update({message.message_id: message for message in messages})
            return messages

    async def reclaim(
        self, consumer: str, *, min_idle_ms: int, count: int
    ) -> tuple[TaskStreamMessage, ...]:
        del consumer, min_idle_ms, count
        return ()

    async def ack(self, message_id: str) -> None:
        self._inflight.pop(message_id, None)

    async def dead_letter(self, message: TaskStreamMessage, error: str) -> None:
        self._inflight.pop(message.message_id, None)
        self._dead_letters.append((message, error))

    async def close(self) -> None:
        return None

    @property
    def dead_letters(self) -> tuple[tuple[TaskStreamMessage, str], ...]:
        return tuple(self._dead_letters)


class RedisTaskStream:
    """Redis Streams Consumer Group adapter with explicit PEL reclaim and DLQ."""

    def __init__(
        self,
        redis_url: str,
        *,
        stream_name: str,
        consumer_group: str,
        dead_letter_stream: str | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._stream_name = stream_name
        self._group = consumer_group
        self._dead_letter_stream = dead_letter_stream or f"{stream_name}:dlq"
        self._client: object | None = None
        self._ready = False

    async def publish(
        self,
        task_id: str,
        tenant_id: str,
        traceparent: str | None,
        event_type: str | None = None,
    ) -> str:
        client = await self._get_client()
        result = await client.xadd(
            self._stream_name,
            {
                "task_id": task_id,
                "tenant_id": tenant_id,
                "traceparent": traceparent or "",
                "event_type": event_type or "",
            },
        )
        return self._decode(result)

    async def consume(
        self, consumer: str, *, count: int, block_ms: int
    ) -> tuple[TaskStreamMessage, ...]:
        client = await self._get_client()
        result = await client.xreadgroup(
            self._group,
            consumer,
            {self._stream_name: ">"},
            count=count,
            block=block_ms,
        )
        return self._parse_read(result)

    async def reclaim(
        self, consumer: str, *, min_idle_ms: int, count: int
    ) -> tuple[TaskStreamMessage, ...]:
        client = await self._get_client()
        result = await client.xautoclaim(
            self._stream_name,
            self._group,
            consumer,
            min_idle_time=min_idle_ms,
            start_id="0-0",
            count=count,
        )
        messages = (
            result[1] if isinstance(result, (tuple, list)) and len(result) > 1 else []
        )
        return tuple(
            self._to_message(message_id, values, deliveries=2)
            for message_id, values in messages
        )

    async def ack(self, message_id: str) -> None:
        client = await self._get_client()
        await client.xack(self._stream_name, self._group, message_id)

    async def dead_letter(self, message: TaskStreamMessage, error: str) -> None:
        client = await self._get_client()
        await client.xadd(
            self._dead_letter_stream,
            {
                "source_message_id": message.message_id,
                "task_id": message.task_id,
                "tenant_id": message.tenant_id,
                "traceparent": message.traceparent or "",
                "error": error[:2000],
            },
        )
        await client.xack(self._stream_name, self._group, message.message_id)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._ready = False

    async def _get_client(self):
        if self._client is None:
            try:
                import redis.asyncio as redis
            except ImportError as exc:
                raise RuntimeError(
                    "redis package is required for RedisTaskStream"
                ) from exc
            self._client = redis.Redis.from_url(
                self._redis_url, decode_responses=False, protocol=2
            )
        if not self._ready:
            try:
                await self._client.xgroup_create(
                    self._stream_name, self._group, id="0-0", mkstream=True
                )
            except Exception as exc:
                if "BUSYGROUP" not in str(exc):
                    raise
            self._ready = True
        return self._client

    def _parse_read(self, result: object) -> tuple[TaskStreamMessage, ...]:
        messages: list[TaskStreamMessage] = []
        for _, entries in result or []:
            for message_id, values in entries:
                messages.append(self._to_message(message_id, values))
        return tuple(messages)

    def _to_message(
        self, message_id: object, values: object, *, deliveries: int = 1
    ) -> TaskStreamMessage:
        fields = {
            self._decode(key): self._decode(value)
            for key, value in dict(values).items()
        }
        task_id = fields.get("task_id", "")
        tenant_id = fields.get("tenant_id", "")
        if not task_id or not tenant_id:
            raise ValueError("task stream message is missing task_id or tenant_id")
        traceparent = fields.get("traceparent") or None
        event_type = fields.get("event_type") or None
        return TaskStreamMessage(
            message_id=self._decode(message_id),
            task_id=task_id,
            tenant_id=tenant_id,
            traceparent=traceparent,
            event_type=event_type,
            deliveries=deliveries,
        )

    @staticmethod
    def _decode(value: object) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)
