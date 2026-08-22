"""Deliver Runtime Outbox records to a stream without domain knowledge."""

from __future__ import annotations

from typing import Any

from athena.api.repositories.outbox_repository import OutboxRepository


class OutboxRelay:
    """Publish claimed events and release failed deliveries for retry."""

    def __init__(
        self, repository: OutboxRepository, stream: Any, *, owner: str
    ) -> None:
        if not owner.strip():
            raise ValueError("owner must be non-empty")
        self._repository = repository
        self._stream = stream
        self._owner = owner

    async def dispatch_once(self, *, limit: int = 100) -> int:
        messages = await self._repository.claim(self._owner, limit=limit)
        delivered = 0
        for message in messages:
            try:
                await self._stream.publish(
                    message.payload.get("task_id", message.aggregate_id),
                    message.tenant_id,
                    message.traceparent,
                    message.event_type,
                )
            except Exception as exc:  # noqa: BLE001 - release for a later retry
                await self._repository.release(
                    message.message_id, self._owner, str(exc)
                )
                continue
            if await self._repository.mark_published(message.message_id, self._owner):
                delivered += 1
        return delivered
