"""Tenant-scoped persistence port for Runtime outbox delivery."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .models import OutboxMessageModel


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    tenant_id: str
    aggregate_id: str
    event_type: str
    payload: dict[str, object]
    traceparent: str | None


class OutboxRepository:
    """Claim and finalize durable Runtime events for a stream adapter."""

    def __init__(
        self, sessions: async_sessionmaker[AsyncSession], *, lease_seconds: int = 30
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._sessions = sessions
        self._lease = timedelta(seconds=lease_seconds)

    async def claim(self, owner: str, *, limit: int = 100) -> tuple[OutboxMessage, ...]:
        if not owner.strip():
            raise ValueError("owner must be non-empty")
        now = datetime.now(UTC)
        async with self._sessions() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(OutboxMessageModel)
                        .where(
                            OutboxMessageModel.published_at.is_(None),
                            OutboxMessageModel.available_at <= now,
                            (
                                OutboxMessageModel.locked_until.is_(None)
                                | (OutboxMessageModel.locked_until <= now)
                            ),
                        )
                        .order_by(OutboxMessageModel.created_at)
                        .limit(max(1, limit))
                        .with_for_update()
                    )
                ).all()
                for row in rows:
                    row.attempts += 1
                    row.lock_owner = owner
                    row.locked_until = now + self._lease
                return tuple(self._from_model(row) for row in rows)

    async def mark_published(self, message_id: str, owner: str) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(OutboxMessageModel)
                    .where(
                        OutboxMessageModel.id == message_id,
                        OutboxMessageModel.lock_owner == owner,
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                row.published_at = datetime.now(UTC)
                row.lock_owner = None
                row.locked_until = None
                return True

    async def release(self, message_id: str, owner: str, error: str) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(OutboxMessageModel)
                    .where(
                        OutboxMessageModel.id == message_id,
                        OutboxMessageModel.lock_owner == owner,
                    )
                    .with_for_update()
                )
                if row is None:
                    return False
                row.lock_owner = None
                row.locked_until = None
                row.last_error = error[:2000]
                return True

    @staticmethod
    def _from_model(row: OutboxMessageModel) -> OutboxMessage:
        return OutboxMessage(
            message_id=row.id,
            tenant_id=row.tenant_id,
            aggregate_id=row.aggregate_id,
            event_type=row.event_type,
            payload=dict(row.payload_json or {}),
            traceparent=row.traceparent,
        )
