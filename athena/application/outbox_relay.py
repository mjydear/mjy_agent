"""Publish transactional outbox records to the task stream."""

from __future__ import annotations

import logging

from athena.api.repositories import OutboxRepository
from athena.infra.task_stream import TaskStream

logger = logging.getLogger(__name__)


class OutboxRelay:
    def __init__(
        self,
        outbox: OutboxRepository,
        stream: TaskStream,
        *,
        owner: str,
        max_attempts: int = 5,
    ) -> None:
        self._outbox = outbox
        self._stream = stream
        self._owner = owner
        self._max_attempts = max_attempts

    async def dispatch_once(self, limit: int = 16) -> int:
        published = 0
        for message in await self._outbox.claim_batch(self._owner, limit=limit):
            try:
                task_id = str(message.payload["task_id"])
                await self._stream.publish(
                    task_id, message.tenant_id, message.traceparent
                )
                if await self._outbox.mark_published(message.message_id, self._owner):
                    published += 1
            except Exception as exc:  # noqa: BLE001 - retry is the relay contract
                if message.attempts >= self._max_attempts:
                    logger.error(
                        "outbox message exhausted retries id=%s error=%s",
                        message.message_id,
                        exc,
                    )
                await self._outbox.retry(
                    message.message_id,
                    self._owner,
                    str(exc),
                    delay_seconds=min(60.0, 2 ** min(message.attempts, 6)),
                )
        return published
