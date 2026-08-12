"""Evidence metadata repository backed by PostgreSQL and content-addressed storage."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import EvidenceModel
from athena.infra.evidence_content import LocalEvidenceContentStore


@dataclass(frozen=True)
class PersistedEvidence:
    evidence_id: str
    tenant_id: str
    task_id: str
    evidence_type: str
    source: str
    data_origin: str
    summary: str
    content_hash: str
    content_ref: str
    observed_at: datetime


class EvidenceRepository:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        content_store: LocalEvidenceContentStore,
    ) -> None:
        self._sessions = session_factory
        self._content = content_store

    async def create(
        self,
        *,
        tenant_id: str,
        task_id: str,
        evidence_type: str,
        source: str,
        data_origin: str,
        summary: str,
        content: object,
        observed_at: datetime | None = None,
    ) -> PersistedEvidence:
        content_hash, content_ref = await self._content.put(tenant_id, task_id, content)
        evidence = EvidenceModel(
            id=f"evidence-{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            task_id=task_id,
            evidence_type=evidence_type,
            source=source,
            data_origin=data_origin,
            summary=summary[:2000],
            content_hash=content_hash,
            content_ref=content_ref,
            observed_at=observed_at or datetime.now(UTC),
        )
        async with self._sessions() as session:
            async with session.begin():
                session.add(evidence)
        return self._from_model(evidence)

    async def list_for_task(
        self, tenant_id: str, task_id: str
    ) -> tuple[PersistedEvidence, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(EvidenceModel)
                    .where(
                        EvidenceModel.tenant_id == tenant_id,
                        EvidenceModel.task_id == task_id,
                    )
                    .order_by(EvidenceModel.observed_at, EvidenceModel.id)
                )
            ).all()
            return tuple(self._from_model(row) for row in rows)

    async def get_content(self, tenant_id: str, evidence_id: str) -> object | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(EvidenceModel).where(
                    EvidenceModel.tenant_id == tenant_id,
                    EvidenceModel.id == evidence_id,
                )
            )
        return None if row is None else await self._content.get(row.content_ref)

    async def verify_content_hash(self, tenant_id: str, evidence_id: str) -> bool:
        async with self._sessions() as session:
            row = await session.scalar(
                select(EvidenceModel).where(
                    EvidenceModel.tenant_id == tenant_id,
                    EvidenceModel.id == evidence_id,
                )
            )
        if row is None:
            return False
        return await self._content.verify(row.content_ref, row.content_hash)

    @staticmethod
    def _from_model(row: EvidenceModel) -> PersistedEvidence:
        observed_at = (
            row.observed_at.replace(tzinfo=UTC)
            if row.observed_at.tzinfo is None
            else row.observed_at.astimezone(UTC)
        )
        return PersistedEvidence(
            evidence_id=row.id,
            tenant_id=row.tenant_id,
            task_id=row.task_id,
            evidence_type=row.evidence_type,
            source=row.source,
            data_origin=row.data_origin,
            summary=row.summary,
            content_hash=row.content_hash,
            content_ref=row.content_ref,
            observed_at=observed_at,
        )
