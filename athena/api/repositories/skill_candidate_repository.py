"""Tenant-scoped durable repository for offline Skill Candidates."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from uuid import uuid4

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
from athena.learning.candidate_generation import CandidateGenerationRun
from athena.learning.skill_validation import SKILL_CANDIDATE_SCHEMA_VERSION
from athena.learning.skill_validation import (
    CandidateValidationCategory,
    CandidateValidationReport,
    CandidateValidationViolation,
)
from athena.runtime.learning import (
    TrajectoryAdmission,
    TrajectoryStatus,
    TrajectorySummary,
)

from .models import (
    LearningTrajectoryEventModel,
    LearningTrajectoryModel,
    SkillCandidateGenerationRunModel,
    SkillCandidateValidationReportModel,
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
        schema_version: str = SKILL_CANDIDATE_SCHEMA_VERSION,
        skill_id: str | None = None,
        version: int = 1,
        description: str = "",
        trigger: dict[str, object] | None = None,
        allowed_tools: tuple[str, ...] = (),
        failure_recovery: tuple[str, ...] = (),
        success_contract: dict[str, object] | None = None,
        evidence_requirements: tuple[str, ...] = (),
        token_budget_hint: int = 0,
        source_trajectory_ids: tuple[str, ...] = (),
        evaluation_status: str = "not_evaluated",
        risk_level: str = "S1",
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
                        schema_version=schema_version,
                        skill_id=skill_id or candidate_id,
                        version=version,
                        description=description,
                        trigger_json=dict(trigger or {}),
                        allowed_tools_json=list(allowed_tools),
                        failure_recovery_json=list(failure_recovery),
                        success_contract_json=dict(success_contract or {}),
                        evidence_requirements_json=list(evidence_requirements),
                        token_budget_hint=token_budget_hint,
                        source_trajectory_ids_json=list(source_trajectory_ids),
                        evaluation_status=evaluation_status,
                        risk_level=risk_level,
                        audit_events_json=[
                            {
                                "kind": "candidate.created",
                                "at": datetime.now(UTC).isoformat(),
                                "from_status": (
                                    TrajectoryStatus.ELIGIBLE.value
                                    if source_trajectory_ids
                                    else None
                                ),
                                "to_status": CANDIDATE_STATUS,
                                "source_trajectory_ids": list(source_trajectory_ids),
                                "activation_allowed": False,
                            }
                        ],
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

    async def save_trajectory(self, summary: TrajectorySummary) -> TrajectorySummary:
        """Persist one immutable redacted summary and both admission events."""

        if summary.contains_raw_artifacts or summary.contains_hidden_reasoning:
            raise ValueError("unsafe trajectory payload cannot be persisted")
        try:
            async with self._sessions() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(LearningTrajectoryModel).where(
                            LearningTrajectoryModel.tenant_id == summary.tenant_id,
                            LearningTrajectoryModel.source_task_id
                            == summary.source_task_id,
                        )
                    )
                    if existing is not None:
                        return self._trajectory_from_model(existing)
                    admission = self._admission_json(summary.admission)
                    model = LearningTrajectoryModel(
                        id=summary.trajectory_id,
                        tenant_id=summary.tenant_id,
                        source_task_id=summary.source_task_id,
                        schema_version=summary.schema_version,
                        status=summary.status.value,
                        task_summary=summary.task_summary,
                        outcome_summary_json=dict(summary.outcome_summary),
                        tool_calls_json=[dict(item) for item in summary.tool_calls],
                        evidence_json=[dict(item) for item in summary.evidence],
                        usage_json=dict(summary.usage),
                        budget_json=dict(summary.budget),
                        admission_json=admission,
                        quality_score=summary.admission.quality_score,
                        rejection_reasons_json=list(
                            summary.admission.rejection_reasons
                        ),
                        redaction_count=summary.redaction_count,
                        contains_raw_artifacts=False,
                        contains_hidden_reasoning=False,
                        admitted_at=summary.created_at,
                        created_at=summary.created_at,
                        updated_at=summary.created_at,
                    )
                    session.add(model)
                    session.add_all(
                        [
                            LearningTrajectoryEventModel(
                                id=f"trajectory-event-{uuid4().hex}",
                                tenant_id=summary.tenant_id,
                                trajectory_id=summary.trajectory_id,
                                kind="trajectory.observed",
                                from_status=None,
                                to_status=TrajectoryStatus.OBSERVED.value,
                                details_json={
                                    "schema_version": summary.schema_version,
                                    "raw_artifacts_included": False,
                                    "hidden_reasoning_included": False,
                                },
                                created_at=summary.created_at,
                            ),
                            LearningTrajectoryEventModel(
                                id=f"trajectory-event-{uuid4().hex}",
                                tenant_id=summary.tenant_id,
                                trajectory_id=summary.trajectory_id,
                                kind="trajectory.admitted",
                                from_status=TrajectoryStatus.OBSERVED.value,
                                to_status=summary.status.value,
                                details_json={
                                    "quality_score": summary.admission.quality_score,
                                    "rejection_reasons": list(
                                        summary.admission.rejection_reasons
                                    ),
                                },
                                created_at=summary.created_at,
                            ),
                        ]
                    )
                    await session.flush()
                    return self._trajectory_from_model(model)
        except IntegrityError:
            existing = await self.get_trajectory(
                summary.tenant_id, summary.trajectory_id
            )
            if existing is None:
                raise
            return existing

    async def get_trajectory(
        self, tenant_id: str, trajectory_id: str
    ) -> TrajectorySummary | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(LearningTrajectoryModel).where(
                    LearningTrajectoryModel.tenant_id == tenant_id,
                    LearningTrajectoryModel.id == trajectory_id,
                )
            )
            return None if model is None else self._trajectory_from_model(model)

    async def list_trajectory_events(
        self, tenant_id: str, trajectory_id: str
    ) -> tuple[dict[str, object], ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(LearningTrajectoryEventModel)
                    .where(
                        LearningTrajectoryEventModel.tenant_id == tenant_id,
                        LearningTrajectoryEventModel.trajectory_id == trajectory_id,
                    )
                    .order_by(LearningTrajectoryEventModel.created_at)
                )
            ).all()
            return tuple(
                {
                    "kind": row.kind,
                    "from_status": row.from_status,
                    "to_status": row.to_status,
                    "details": dict(row.details_json or {}),
                    "created_at": (
                        utc_or_none(row.created_at).isoformat()
                        if utc_or_none(row.created_at)
                        else None
                    ),
                }
                for row in rows
            )

    async def get(self, tenant_id: str, candidate_id: str) -> SkillCandidate | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillCandidateModel).where(
                    SkillCandidateModel.tenant_id == tenant_id,
                    SkillCandidateModel.id == candidate_id,
                )
            )
            return None if model is None else self._from_model(model)

    async def get_by_source_digest(
        self, tenant_id: str, source_digest: str
    ) -> SkillCandidate | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillCandidateModel).where(
                    SkillCandidateModel.tenant_id == tenant_id,
                    SkillCandidateModel.source_digest == source_digest,
                )
            )
            return None if model is None else self._from_model(model)

    async def list_deduplication_candidates(
        self, tenant_id: str, *, limit: int = 200
    ) -> tuple[SkillCandidate, ...]:
        """Return bounded, non-rejected Candidate projections for rule deduplication."""

        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(SkillCandidateModel)
                    .where(
                        SkillCandidateModel.tenant_id == tenant_id,
                        SkillCandidateModel.status != REJECTED_STATUS,
                    )
                    .order_by(SkillCandidateModel.created_at.desc())
                    .limit(limit)
                )
            ).all()
            return tuple(self._from_model(row) for row in rows)

    async def create_generation_run(
        self, run: CandidateGenerationRun
    ) -> tuple[CandidateGenerationRun, bool]:
        """Elect one generator call per tenant-scoped trajectory source set."""

        try:
            async with self._sessions() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(SkillCandidateGenerationRunModel).where(
                            SkillCandidateGenerationRunModel.tenant_id == run.tenant_id,
                            SkillCandidateGenerationRunModel.source_digest
                            == run.source_digest,
                        )
                    )
                    if existing is not None:
                        return self._generation_from_model(existing), False
                    model = SkillCandidateGenerationRunModel(
                        id=run.run_id,
                        tenant_id=run.tenant_id,
                        source_digest=run.source_digest,
                        source_trajectory_ids_json=list(run.source_trajectory_ids),
                        status=run.status,
                        digest_json=dict(run.digest),
                        generator=run.generator,
                        candidate_id=run.candidate_id,
                        validation_report_id=run.validation_report_id,
                        duplicate_of_candidate_id=run.duplicate_of_candidate_id,
                        deduplication_json=dict(run.deduplication),
                        model=run.model,
                        usage_json=dict(run.usage),
                        latency_ms=run.latency_ms,
                        failure_code=run.failure_code,
                        failure_message=run.failure_message,
                        created_by=run.created_by,
                        created_at=run.created_at,
                        completed_at=run.completed_at,
                    )
                    session.add(model)
                    await session.flush()
                    return self._generation_from_model(model), True
        except IntegrityError:
            existing = await self.get_generation_by_source(
                run.tenant_id, run.source_digest
            )
            if existing is None:
                raise
            return existing, False

    async def complete_generation_run(
        self,
        tenant_id: str,
        run_id: str,
        *,
        status: str,
        candidate_id: str | None = None,
        validation_report_id: str | None = None,
        duplicate_of_candidate_id: str | None = None,
        deduplication: dict[str, object] | None = None,
        model: str | None = None,
        usage: dict[str, int] | None = None,
        latency_ms: int | None = None,
        failure_code: str | None = None,
        failure_message: str | None = None,
    ) -> CandidateGenerationRun | None:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.scalar(
                    select(SkillCandidateGenerationRunModel)
                    .where(
                        SkillCandidateGenerationRunModel.tenant_id == tenant_id,
                        SkillCandidateGenerationRunModel.id == run_id,
                    )
                    .with_for_update()
                )
                if row is None:
                    return None
                if row.status != "started":
                    return self._generation_from_model(row)
                row.status = status
                row.candidate_id = candidate_id
                row.validation_report_id = validation_report_id
                row.duplicate_of_candidate_id = duplicate_of_candidate_id
                row.deduplication_json = dict(deduplication or {})
                row.model = model
                row.usage_json = dict(usage or {})
                row.latency_ms = latency_ms
                row.failure_code = failure_code
                row.failure_message = failure_message
                row.completed_at = datetime.now(UTC)
                await session.flush()
                return self._generation_from_model(row)

    async def get_generation(
        self, tenant_id: str, run_id: str
    ) -> CandidateGenerationRun | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SkillCandidateGenerationRunModel).where(
                    SkillCandidateGenerationRunModel.tenant_id == tenant_id,
                    SkillCandidateGenerationRunModel.id == run_id,
                )
            )
            return None if row is None else self._generation_from_model(row)

    async def get_generation_by_source(
        self, tenant_id: str, source_digest: str
    ) -> CandidateGenerationRun | None:
        async with self._sessions() as session:
            row = await session.scalar(
                select(SkillCandidateGenerationRunModel).where(
                    SkillCandidateGenerationRunModel.tenant_id == tenant_id,
                    SkillCandidateGenerationRunModel.source_digest == source_digest,
                )
            )
            return None if row is None else self._generation_from_model(row)

    async def record_validation(
        self, report: CandidateValidationReport
    ) -> CandidateValidationReport:
        """Persist one deterministic report and apply its non-Active outcome."""

        async with self._sessions() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(SkillCandidateValidationReportModel).where(
                        SkillCandidateValidationReportModel.tenant_id
                        == report.tenant_id,
                        SkillCandidateValidationReportModel.id == report.report_id,
                    )
                )
                if existing is not None:
                    return self._validation_from_model(existing)
                candidate = await session.scalar(
                    select(SkillCandidateModel)
                    .where(
                        SkillCandidateModel.tenant_id == report.tenant_id,
                        SkillCandidateModel.id == report.candidate_id,
                    )
                    .with_for_update()
                )
                if candidate is None:
                    raise SkillCandidateLifecycleError("SKILL_CANDIDATE_NOT_FOUND")
                if candidate.status not in {CANDIDATE_STATUS, REJECTED_STATUS}:
                    raise SkillCandidateLifecycleError(
                        "SKILL_CANDIDATE_VALIDATION_STATE_INVALID"
                    )
                if candidate.status == REJECTED_STATUS and report.passed:
                    raise SkillCandidateLifecycleError("SKILL_CANDIDATE_REJECTED_FINAL")
                target_status = CANDIDATE_STATUS if report.passed else REJECTED_STATUS
                previous_status = candidate.status
                candidate.status = target_status
                candidate.evaluation_status = (
                    "validation_passed" if report.passed else "validation_failed"
                )
                if not report.passed:
                    candidate.decided_at = report.validated_at
                candidate.audit_events_json = [
                    *(candidate.audit_events_json or []),
                    {
                        "kind": "candidate.validated",
                        "at": report.validated_at.isoformat(),
                        "report_id": report.report_id,
                        "from_status": previous_status,
                        "to_status": target_status,
                        "evaluation_status": candidate.evaluation_status,
                        "schema_valid": report.schema_valid,
                        "security_valid": report.security_valid,
                        "violation_codes": [item.code for item in report.violations],
                        "activation_allowed": False,
                    },
                ]
                model = SkillCandidateValidationReportModel(
                    id=report.report_id,
                    tenant_id=report.tenant_id,
                    candidate_id=report.candidate_id,
                    candidate_digest=report.candidate_digest,
                    validator_version=report.validator_version,
                    schema_valid=report.schema_valid,
                    security_valid=report.security_valid,
                    passed=report.passed,
                    checks_json=dict(report.checks),
                    violations_json=[item.to_dict() for item in report.violations],
                    validated_at=report.validated_at,
                )
                session.add(model)
                await session.flush()
                return self._validation_from_model(model)

    async def get_validation(
        self, tenant_id: str, report_id: str
    ) -> CandidateValidationReport | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillCandidateValidationReportModel).where(
                    SkillCandidateValidationReportModel.tenant_id == tenant_id,
                    SkillCandidateValidationReportModel.id == report_id,
                )
            )
            return None if model is None else self._validation_from_model(model)

    async def latest_validation_for_candidate(
        self, tenant_id: str, candidate_id: str
    ) -> CandidateValidationReport | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillCandidateValidationReportModel)
                .where(
                    SkillCandidateValidationReportModel.tenant_id == tenant_id,
                    SkillCandidateValidationReportModel.candidate_id == candidate_id,
                )
                .order_by(SkillCandidateValidationReportModel.validated_at.desc())
                .limit(1)
            )
            return None if model is None else self._validation_from_model(model)

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

    async def mark_review_pending_after_shadow(
        self, tenant_id: str, candidate_id: str
    ) -> SkillCandidate | None:
        """Move a replay-passed Candidate to the human review queue.

        Shadow persistence records the report first.  This separate transition
        keeps failed Shadow runs candidate-only and gives release a durable,
        auditable precondition to check.
        """

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
                if model.status == REVIEW_PENDING_STATUS:
                    return self._from_model(model)
                if (
                    model.status != CANDIDATE_STATUS
                    or model.evaluation_status != "replay_ab_passed"
                    or not model.shadow_report_id
                ):
                    raise SkillCandidateLifecycleError(
                        "SKILL_CANDIDATE_SHADOW_REVIEW_PRECONDITION_REQUIRED"
                    )
                previous_status = model.status
                model.status = REVIEW_PENDING_STATUS
                model.audit_events_json = [
                    *(model.audit_events_json or []),
                    {
                        "kind": "candidate.shadow_review_ready",
                        "at": datetime.now(UTC).isoformat(),
                        "from_status": previous_status,
                        "to_status": REVIEW_PENDING_STATUS,
                        "shadow_report_id": model.shadow_report_id,
                        "activation_allowed": False,
                    },
                ]
                return self._from_model(model)

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
                previous_status = model.status
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
                model.audit_events_json = [
                    *(model.audit_events_json or []),
                    {
                        "kind": "candidate.transition",
                        "at": datetime.now(UTC).isoformat(),
                        "from_status": previous_status,
                        "to_status": target,
                        "activation_allowed": False,
                    },
                ]
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
            schema_version=model.schema_version,
            skill_id=model.skill_id,
            version=model.version,
            description=model.description,
            trigger=dict(model.trigger_json or {}),
            allowed_tools=tuple(model.allowed_tools_json or ()),
            failure_recovery=tuple(model.failure_recovery_json or ()),
            success_contract=dict(model.success_contract_json or {}),
            evidence_requirements=tuple(model.evidence_requirements_json or ()),
            token_budget_hint=model.token_budget_hint,
            source_trajectory_ids=tuple(model.source_trajectory_ids_json or ()),
            evaluation_status=model.evaluation_status,
            risk_level=model.risk_level,
            audit_events=tuple(model.audit_events_json or ()),
            replay_report_id=model.replay_report_id,
            shadow_report_id=model.shadow_report_id,
            reviewed_by=model.reviewed_by,
            review_note=model.review_note,
            decided_at=utc_or_none(model.decided_at),
        )

    @staticmethod
    def _admission_json(admission: TrajectoryAdmission) -> dict[str, object]:
        return {
            "eligible": admission.eligible,
            "rejection_reasons": list(admission.rejection_reasons),
            "quality_score": admission.quality_score,
            "quality_factors": dict(admission.quality_factors),
            "quality_explanations": list(admission.quality_explanations),
            "checks": dict(admission.checks),
        }

    @staticmethod
    def _validation_from_model(
        model: SkillCandidateValidationReportModel,
    ) -> CandidateValidationReport:
        return CandidateValidationReport(
            report_id=model.id,
            tenant_id=model.tenant_id,
            candidate_id=model.candidate_id,
            candidate_digest=model.candidate_digest,
            validator_version=model.validator_version,
            schema_valid=model.schema_valid,
            security_valid=model.security_valid,
            passed=model.passed,
            checks={
                str(key): bool(value)
                for key, value in dict(model.checks_json or {}).items()
            },
            violations=tuple(
                CandidateValidationViolation(
                    code=str(item.get("code") or "CANDIDATE_VALIDATION_FAILED"),
                    category=CandidateValidationCategory(
                        str(item.get("category") or "schema")
                    ),
                    field=str(item.get("field") or "candidate"),
                    message=str(item.get("message") or "Candidate validation failed."),
                )
                for item in (model.violations_json or [])
            ),
            validated_at=utc_or_none(model.validated_at) or datetime.now(UTC),
        )

    @staticmethod
    def _generation_from_model(
        model: SkillCandidateGenerationRunModel,
    ) -> CandidateGenerationRun:
        return CandidateGenerationRun(
            run_id=model.id,
            tenant_id=model.tenant_id,
            source_digest=model.source_digest,
            source_trajectory_ids=tuple(model.source_trajectory_ids_json or ()),
            status=model.status,
            digest=dict(model.digest_json or {}),
            generator=model.generator,
            candidate_id=model.candidate_id,
            validation_report_id=model.validation_report_id,
            duplicate_of_candidate_id=model.duplicate_of_candidate_id,
            deduplication=dict(model.deduplication_json or {}),
            model=model.model,
            usage={
                str(key): int(value)
                for key, value in dict(model.usage_json or {}).items()
                if isinstance(value, int) and not isinstance(value, bool)
            },
            latency_ms=model.latency_ms,
            failure_code=model.failure_code,
            failure_message=model.failure_message,
            created_by=model.created_by,
            created_at=utc_or_none(model.created_at) or datetime.now(UTC),
            completed_at=utc_or_none(model.completed_at),
        )

    @staticmethod
    def _trajectory_from_model(model: LearningTrajectoryModel) -> TrajectorySummary:
        admission_raw = dict(model.admission_json or {})
        admission = TrajectoryAdmission(
            eligible=bool(admission_raw.get("eligible")),
            rejection_reasons=tuple(
                str(item) for item in admission_raw.get("rejection_reasons", [])
            ),
            quality_score=float(admission_raw.get("quality_score", 0.0)),
            quality_factors={
                str(key): float(value)
                for key, value in dict(
                    admission_raw.get("quality_factors") or {}
                ).items()
            },
            quality_explanations=tuple(
                str(item) for item in admission_raw.get("quality_explanations", [])
            ),
            checks={
                str(key): bool(value)
                for key, value in dict(admission_raw.get("checks") or {}).items()
            },
        )
        return TrajectorySummary(
            trajectory_id=model.id,
            tenant_id=model.tenant_id,
            source_task_id=model.source_task_id,
            schema_version=model.schema_version,
            status=TrajectoryStatus(model.status),
            task_summary=model.task_summary,
            outcome_summary={
                str(key): str(value)
                for key, value in dict(model.outcome_summary_json or {}).items()
            },
            tool_calls=tuple(dict(item) for item in (model.tool_calls_json or [])),
            evidence=tuple(
                {str(key): str(value) for key, value in dict(item).items()}
                for item in (model.evidence_json or [])
            ),
            usage=dict(model.usage_json or {}),
            budget=dict(model.budget_json or {}),
            admission=admission,
            redaction_count=model.redaction_count,
            created_at=utc_or_none(model.created_at) or datetime.now(UTC),
            contains_raw_artifacts=model.contains_raw_artifacts,
            contains_hidden_reasoning=model.contains_hidden_reasoning,
        )


__all__ = ["SkillCandidateRepository"]
