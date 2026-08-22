"""Durable capture and lease operations for production-shaped Shadow traffic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.evaluation.shadow_traffic import (
    ShadowTraceEnvelope,
    ShadowTrafficResult,
)

from .models import OutboxMessageModel, ShadowTrafficObservationModel


@dataclass(frozen=True)
class ShadowTrafficObservation:
    observation_id: str
    envelope: ShadowTraceEnvelope
    status: str
    attempt_count: int
    baseline_metrics: dict[str, object]
    candidate_metrics: dict[str, object]
    comparison: dict[str, object]
    failure_code: str | None
    started_at: datetime | None
    completed_at: datetime | None

    def to_dict(self) -> dict[str, object]:
        return {
            "observation_id": self.observation_id,
            "tenant_id": self.envelope.tenant_id,
            "trace_id": self.envelope.trace_id,
            "candidate_id": self.envelope.candidate_id,
            "candidate_digest": self.envelope.candidate_digest,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "envelope": self.envelope.to_dict(),
            "baseline_metrics": dict(self.baseline_metrics),
            "candidate_metrics": dict(self.candidate_metrics),
            "comparison": dict(self.comparison),
            "failure_code": self.failure_code,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }


class ShadowTrafficRepository:
    """Persist redacted observations and enqueue them transactionally."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._sessions = session_factory
        self._lease = timedelta(seconds=lease_seconds)

    async def capture(self, envelope: ShadowTraceEnvelope) -> ShadowTrafficObservation:
        try:
            async with self._sessions() as session:
                async with session.begin():
                    existing = await self._find_model(session, envelope)
                    if existing is not None:
                        return self._from_model(existing)
                    model = ShadowTrafficObservationModel(
                        id=envelope.observation_id,
                        tenant_id=envelope.tenant_id,
                        trace_id=envelope.trace_id,
                        candidate_id=envelope.candidate_id,
                        candidate_digest=envelope.candidate_digest,
                        envelope_json=envelope.to_dict(),
                        status="pending",
                        attempt_count=0,
                        baseline_metrics_json=envelope.baseline.to_dict(),
                        candidate_metrics_json={},
                        comparison_json={},
                    )
                    session.add(model)
                    session.add(
                        OutboxMessageModel(
                            id=f"shadow-outbox-{uuid4().hex}",
                            tenant_id=envelope.tenant_id,
                            aggregate_id=envelope.observation_id,
                            event_type="shadow.traffic.captured",
                            payload_json={
                                "task_id": envelope.observation_id,
                                "observation_id": envelope.observation_id,
                            },
                            traceparent=envelope.traceparent,
                            attempts=0,
                            available_at=_now(),
                            created_at=_now(),
                        )
                    )
                    await session.flush()
                    return self._from_model(model)
        except IntegrityError:
            async with self._sessions() as session:
                existing = await self._find_model(session, envelope)
                if existing is None:
                    raise
                return self._from_model(existing)

    async def get(
        self, tenant_id: str, observation_id: str
    ) -> ShadowTrafficObservation | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(ShadowTrafficObservationModel).where(
                    ShadowTrafficObservationModel.tenant_id == tenant_id,
                    ShadowTrafficObservationModel.id == observation_id,
                )
            )
            return None if model is None else self._from_model(model)

    async def claim(
        self, tenant_id: str, observation_id: str, owner: str
    ) -> ShadowTrafficObservation | None:
        if not owner.strip():
            raise ValueError("owner must be non-empty")
        now = _now()
        async with self._sessions() as session:
            async with session.begin():
                model = await session.scalar(
                    select(ShadowTrafficObservationModel)
                    .where(
                        ShadowTrafficObservationModel.tenant_id == tenant_id,
                        ShadowTrafficObservationModel.id == observation_id,
                    )
                    .with_for_update()
                )
                if model is None or model.status == "succeeded":
                    return None
                lease_live = (
                    model.lease_expires_at is not None
                    and _as_utc(model.lease_expires_at) > now
                )
                if model.lease_owner not in {None, owner} and lease_live:
                    return None
                model.status = "running"
                model.attempt_count += 1
                model.lease_owner = owner
                model.lease_expires_at = now + self._lease
                model.started_at = model.started_at or now
                return self._from_model(model)

    async def complete(
        self,
        tenant_id: str,
        observation_id: str,
        owner: str,
        result: ShadowTrafficResult,
    ) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                model = await self._owned_model(
                    session, tenant_id, observation_id, owner
                )
                if model is None:
                    return False
                model.status = result.status
                model.candidate_metrics_json = dict(result.candidate_metrics)
                model.comparison_json = dict(result.comparison)
                model.failure_code = result.failure_code
                model.completed_at = result.completed_at
                model.lease_owner = None
                model.lease_expires_at = None
                return True

    async def fail(
        self, tenant_id: str, observation_id: str, owner: str, failure_code: str
    ) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                model = await self._owned_model(
                    session, tenant_id, observation_id, owner
                )
                if model is None:
                    return False
                model.status = "failed"
                model.failure_code = failure_code[:120]
                model.candidate_metrics_json = {}
                model.comparison_json = {}
                model.completed_at = _now()
                model.lease_owner = None
                model.lease_expires_at = None
                return True

    async def _find_model(
        self, session: AsyncSession, envelope: ShadowTraceEnvelope
    ) -> ShadowTrafficObservationModel | None:
        return await session.scalar(
            select(ShadowTrafficObservationModel).where(
                ShadowTrafficObservationModel.tenant_id == envelope.tenant_id,
                ShadowTrafficObservationModel.trace_id == envelope.trace_id,
                ShadowTrafficObservationModel.candidate_id == envelope.candidate_id,
                ShadowTrafficObservationModel.candidate_digest
                == envelope.candidate_digest,
            )
        )

    async def _owned_model(
        self,
        session: AsyncSession,
        tenant_id: str,
        observation_id: str,
        owner: str,
    ) -> ShadowTrafficObservationModel | None:
        model = await session.scalar(
            select(ShadowTrafficObservationModel)
            .where(
                ShadowTrafficObservationModel.tenant_id == tenant_id,
                ShadowTrafficObservationModel.id == observation_id,
                ShadowTrafficObservationModel.lease_owner == owner,
            )
            .with_for_update()
        )
        return model

    @staticmethod
    def _from_model(model: ShadowTrafficObservationModel) -> ShadowTrafficObservation:
        return ShadowTrafficObservation(
            observation_id=model.id,
            envelope=ShadowTraceEnvelope.from_dict(dict(model.envelope_json or {})),
            status=model.status,
            attempt_count=model.attempt_count,
            baseline_metrics=dict(model.baseline_metrics_json or {}),
            candidate_metrics=dict(model.candidate_metrics_json or {}),
            comparison=dict(model.comparison_json or {}),
            failure_code=model.failure_code,
            started_at=_as_utc(model.started_at) if model.started_at else None,
            completed_at=_as_utc(model.completed_at) if model.completed_at else None,
        )


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


__all__ = ["ShadowTrafficObservation", "ShadowTrafficRepository"]
