"""Durable lookup adapter for the Candidate-to-Skill release seam."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import SkillVersionModel
from athena.api.repositories.skill_repository import SkillVersion


class SkillReleaseRepository:
    """Read the existing Skill version table for release idempotency.

    Release records do not need a new table in this phase.  The Candidate ID is
    stored as ``SkillVersion.source_task_id`` by the release application module;
    this adapter turns that existing durable fact into a tenant-scoped lookup.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def find_version_for_candidate(
        self, tenant_id: str, candidate_id: str
    ) -> SkillVersion | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillVersionModel)
                .where(
                    SkillVersionModel.tenant_id == tenant_id,
                    SkillVersionModel.source_task_id == candidate_id,
                )
                .order_by(SkillVersionModel.created_at.desc())
                .limit(1)
            )
        if model is None:
            return None
        return SkillVersion(
            version_id=model.id,
            tenant_id=model.tenant_id,
            skill_id=model.skill_id,
            version=model.version,
            status=model.status,
            manifest=dict(model.manifest_json or {}),
            procedure=dict(model.procedure_json or {}),
            checksum=model.checksum,
            source_task_id=model.source_task_id,
            benchmark_report_id=model.benchmark_report_id,
            created_by=model.created_by,
            reviewed_by=model.reviewed_by,
            review_note=model.review_note,
        )


__all__ = ["SkillReleaseRepository"]
