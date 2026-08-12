"""Tenant-scoped durable repository for offline Skill Candidates."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    REJECTED_STATUS,
    REPLAY_PENDING_STATUS,
    REVIEW_PENDING_STATUS,
    SHADOW_STATUS,
    SkillCandidate,
    SkillCandidateBridge,
    SkillCandidateLifecycleError,
    SkillCandidateModel,
    utc_or_none,
)


class SkillCandidateRepository:
    """Persist candidate facts and enforce transitions inside transactions.

    There is intentionally no ``activate`` method.  The only outbound operation
    is a review-gated bridge payload consumed by a separate human-controlled flow.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_or_get(
        self,
        *,
        candidate_id: str,
        tenant_id: str,
        name: str,
        workflow_type: str,
        environment_type: str,
        capabilities: tuple[str, ...],
        manifest: dict[str, object],
        procedure: dict[str, object],
        source_outcome_id: str,
        source_feedback_id: str,
        evidence_ids: tuple[str, ...],
        source_digest: str,
        source_summary: dict[str, object],
        created_by: str,
    ) -> SkillCandidate:
        """Create one candidate per tenant-scoped source digest, idempotently."""

        try:
            async with self._sessions() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(SkillCandidateModel).where(
                            SkillCandidateModel.tenant_id == tenant_id,
                            SkillCandidateModel.source_digest == source_digest,
                        )
                    )
                    if existing is not None:
                        return self._from_model(existing)
                    model = SkillCandidateModel(
                        id=candidate_id,
                        tenant_id=tenant_id,
                        name=name,
                        workflow_type=workflow_type,
                        environment_type=environment_type,
                        capabilities_json=list(capabilities),
                        manifest_json=dict(manifest),
                        procedure_json=dict(procedure),
                        status=CANDIDATE_STATUS,
                        source_outcome_id=source_outcome_id,
                        source_feedback_id=source_feedback_id,
                        evidence_ids_json=list(evidence_ids),
                        source_digest=source_digest,
                        source_summary_json=dict(source_summary),
                        created_by=created_by,
                    )
                    session.add(model)
                    await session.flush()
                    return self._from_model(model)
        except IntegrityError:
            # A concurrent proposer may win the unique source constraint.
            async with self._sessions() as session:
                existing = await session.scalar(
                    select(SkillCandidateModel).where(
                        SkillCandidateModel.tenant_id == tenant_id,
                        SkillCandidateModel.source_digest == source_digest,
                    )
                )
            if existing is None:
                raise
            return self._from_model(existing)

    async def get(self, tenant_id: str, candidate_id: str) -> SkillCandidate | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillCandidateModel).where(
                    SkillCandidateModel.tenant_id == tenant_id,
                    SkillCandidateModel.id == candidate_id,
                )
            )
            return None if model is None else self._from_model(model)

    async def mark_replay_pending(
        self, tenant_id: str, candidate_id: str
    ) -> SkillCandidate | None:
        return await self._transition(
            tenant_id,
            candidate_id,
            expected=(CANDIDATE_STATUS,),
            target=REPLAY_PENDING_STATUS,
        )

    async def record_replay(
        self,
        tenant_id: str,
        candidate_id: str,
        *,
        report_id: str,
        passed: bool,
    ) -> SkillCandidate | None:
        target = SHADOW_STATUS if passed else REJECTED_STATUS
        return await self._transition(
            tenant_id,
            candidate_id,
            expected=(REPLAY_PENDING_STATUS,),
            target=target,
            replay_report_id=report_id,
            decided_at=None if passed else datetime.now(UTC),
        )

    async def record_shadow(
        self,
        tenant_id: str,
        candidate_id: str,
        *,
        report_id: str,
        passed: bool,
    ) -> SkillCandidate | None:
        target = REVIEW_PENDING_STATUS if passed else REJECTED_STATUS
        return await self._transition(
            tenant_id,
            candidate_id,
            expected=(SHADOW_STATUS,),
            target=target,
            shadow_report_id=report_id,
            decided_at=None if passed else datetime.now(UTC),
        )

    async def reject(
        self,
        tenant_id: str,
        candidate_id: str,
        *,
        reviewed_by: str,
        note: str,
    ) -> SkillCandidate | None:
        return await self._transition(
            tenant_id,
            candidate_id,
            expected=(
                CANDIDATE_STATUS,
                REPLAY_PENDING_STATUS,
                SHADOW_STATUS,
                REVIEW_PENDING_STATUS,
            ),
            target=REJECTED_STATUS,
            reviewed_by=reviewed_by,
            review_note=note,
            decided_at=datetime.now(UTC),
        )

    async def get_bridge(
        self, tenant_id: str, candidate_id: str
    ) -> SkillCandidateBridge | None:
        candidate = await self.get(tenant_id, candidate_id)
        if candidate is None:
            return None
        if candidate.status != REVIEW_PENDING_STATUS:
            raise SkillCandidateLifecycleError("SKILL_CANDIDATE_NOT_REVIEW_READY")
        return SkillCandidateBridge(
            candidate_id=candidate.candidate_id,
            tenant_id=candidate.tenant_id,
            name=candidate.name,
            environment_type=candidate.environment_type,
            capabilities=candidate.capabilities,
            manifest=dict(candidate.manifest),
            procedure=dict(candidate.procedure),
            source_outcome_id=candidate.source_outcome_id,
            source_feedback_id=candidate.source_feedback_id,
            evidence_ids=candidate.evidence_ids,
            replay_report_id=candidate.replay_report_id,
            shadow_report_id=candidate.shadow_report_id,
            audit={
                "action": "manual_human_draft_creation_required",
                "candidate_id": candidate.candidate_id,
                "source_outcome_id": candidate.source_outcome_id,
                "source_feedback_id": candidate.source_feedback_id,
                "evidence_ids": list(candidate.evidence_ids),
                "replay_report_id": candidate.replay_report_id,
                "shadow_report_id": candidate.shadow_report_id,
            },
        )

    async def _transition(
        self,
        tenant_id: str,
        candidate_id: str,
        *,
        expected: Iterable[str],
        target: str,
        replay_report_id: str | None = None,
        shadow_report_id: str | None = None,
        reviewed_by: str | None = None,
        review_note: str | None = None,
        decided_at: datetime | None = None,
    ) -> SkillCandidate | None:
        async with self._sessions() as session:
            async with session.begin():
                model = await session.scalar(
                    select(SkillCandidateModel)
                    .where(
                        SkillCandidateModel.tenant_id == tenant_id,
                        SkillCandidateModel.id == candidate_id,
                    )
                    .with_for_update()
                )
                if model is None:
                    return None
                if model.status not in set(expected):
                    raise SkillCandidateLifecycleError(
                        "SKILL_CANDIDATE_INVALID_TRANSITION"
                    )
                model.status = target
                if replay_report_id is not None:
                    model.replay_report_id = replay_report_id
                if shadow_report_id is not None:
                    model.shadow_report_id = shadow_report_id
                if reviewed_by is not None:
                    model.reviewed_by = reviewed_by
                if review_note is not None:
                    model.review_note = review_note
                if decided_at is not None:
                    model.decided_at = decided_at
                return self._from_model(model)

    @staticmethod
    def _from_model(model: SkillCandidateModel) -> SkillCandidate:
        return SkillCandidate(
            candidate_id=model.id,
            tenant_id=model.tenant_id,
            name=model.name,
            workflow_type=model.workflow_type,
            environment_type=model.environment_type,
            capabilities=tuple(model.capabilities_json or ()),
            manifest=dict(model.manifest_json or {}),
            procedure=dict(model.procedure_json or {}),
            status=model.status,
            source_outcome_id=model.source_outcome_id,
            source_feedback_id=model.source_feedback_id,
            evidence_ids=tuple(model.evidence_ids_json or ()),
            source_digest=model.source_digest,
            source_summary=dict(model.source_summary_json or {}),
            created_by=model.created_by,
            replay_report_id=model.replay_report_id,
            shadow_report_id=model.shadow_report_id,
            reviewed_by=model.reviewed_by,
            review_note=model.review_note,
            decided_at=utc_or_none(model.decided_at),
        )


__all__ = ["SkillCandidateRepository"]
