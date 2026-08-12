"""Tenant-scoped persistence for fixed Skill Baseline observations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.evaluation.skill_replay import BaselineCaseResult, BaselineRun
from athena.evaluation.skill_replay_ab import (
    ReplayABCaseComparison,
    ReplayABRun,
    ReplayGroupMetrics,
)
from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    REJECTED_STATUS,
    SkillCandidateLifecycleError,
    SkillCandidateModel,
)

from .models import SkillBaselineRunModel, SkillReplayABRunModel


class SkillEvaluationRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def save_baseline(self, run: BaselineRun) -> BaselineRun:
        if run.candidate_loaded:
            raise ValueError("a Baseline run cannot contain a Candidate")
        async with self._sessions() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(SkillBaselineRunModel).where(
                        SkillBaselineRunModel.tenant_id == run.tenant_id,
                        SkillBaselineRunModel.id == run.run_id,
                    )
                )
                if existing is not None:
                    return self._from_model(existing)
                model = SkillBaselineRunModel(
                    id=run.run_id,
                    tenant_id=run.tenant_id,
                    schema_version=run.schema_version,
                    case_definition_digest=run.case_definition_digest,
                    runner=run.runner,
                    candidate_loaded=False,
                    case_count=len(run.results),
                    oracle_pass_count=run.oracle_pass_count,
                    results_json=[item.to_dict() for item in run.results],
                    started_at=run.started_at,
                    completed_at=run.completed_at,
                )
                session.add(model)
                await session.flush()
                return self._from_model(model)

    async def get_baseline(self, tenant_id: str, run_id: str) -> BaselineRun | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillBaselineRunModel).where(
                    SkillBaselineRunModel.tenant_id == tenant_id,
                    SkillBaselineRunModel.id == run_id,
                )
            )
            return None if model is None else self._from_model(model)

    async def find_replay_ab(
        self,
        tenant_id: str,
        candidate_id: str,
        candidate_digest: str,
        case_definition_digest: str,
        runner: str,
    ) -> ReplayABRun | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillReplayABRunModel).where(
                    SkillReplayABRunModel.tenant_id == tenant_id,
                    SkillReplayABRunModel.candidate_id == candidate_id,
                    SkillReplayABRunModel.candidate_digest == candidate_digest,
                    SkillReplayABRunModel.case_definition_digest
                    == case_definition_digest,
                    SkillReplayABRunModel.runner == runner,
                )
            )
            return None if model is None else self._replay_ab_from_model(model)

    async def get_replay_ab(self, tenant_id: str, run_id: str) -> ReplayABRun | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(SkillReplayABRunModel).where(
                    SkillReplayABRunModel.tenant_id == tenant_id,
                    SkillReplayABRunModel.id == run_id,
                )
            )
            return None if model is None else self._replay_ab_from_model(model)

    async def save_replay_ab(self, run: ReplayABRun) -> ReplayABRun:
        """Persist one idempotent A/B report and apply its non-Active gate result."""

        try:
            async with self._sessions() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(SkillReplayABRunModel).where(
                            SkillReplayABRunModel.tenant_id == run.tenant_id,
                            SkillReplayABRunModel.id == run.run_id,
                        )
                    )
                    if existing is not None:
                        return self._replay_ab_from_model(existing)
                    candidate = await session.scalar(
                        select(SkillCandidateModel)
                        .where(
                            SkillCandidateModel.tenant_id == run.tenant_id,
                            SkillCandidateModel.id == run.candidate_id,
                        )
                        .with_for_update()
                    )
                    if candidate is None:
                        raise SkillCandidateLifecycleError("SKILL_CANDIDATE_NOT_FOUND")
                    if candidate.status not in {CANDIDATE_STATUS, REJECTED_STATUS}:
                        raise SkillCandidateLifecycleError(
                            "SKILL_CANDIDATE_REPLAY_AB_STATE_INVALID"
                        )
                    previous_status = candidate.status
                    if run.gate_passed:
                        candidate.status = CANDIDATE_STATUS
                        candidate.evaluation_status = "replay_ab_passed"
                        candidate.decided_at = None
                    else:
                        candidate.status = REJECTED_STATUS
                        candidate.evaluation_status = "evaluation_failed"
                        candidate.decided_at = run.completed_at
                    candidate.audit_events_json = [
                        *(candidate.audit_events_json or []),
                        {
                            "kind": "candidate.replay_ab_evaluated",
                            "at": run.completed_at.isoformat(),
                            "run_id": run.run_id,
                            "from_status": previous_status,
                            "to_status": candidate.status,
                            "evaluation_status": candidate.evaluation_status,
                            "gate_passed": run.gate_passed,
                            "failed_gate_checks": [
                                key
                                for key, passed in run.gate_checks.items()
                                if not passed
                            ],
                            "activation_allowed": False,
                        },
                    ]
                    model = SkillReplayABRunModel(
                        id=run.run_id,
                        tenant_id=run.tenant_id,
                        candidate_id=run.candidate_id,
                        candidate_digest=run.candidate_digest,
                        validation_report_id=run.validation_report_id,
                        schema_version=run.schema_version,
                        case_definition_digest=run.case_definition_digest,
                        runner=run.runner,
                        status=run.status,
                        case_count=len(run.comparisons),
                        comparisons_json=[item.to_dict() for item in run.comparisons],
                        aggregate_json={
                            key: dict(value) for key, value in run.aggregate.items()
                        },
                        gate_checks_json=dict(run.gate_checks),
                        gate_passed=run.gate_passed,
                        failure_reason=run.failure_reason,
                        started_at=run.started_at,
                        completed_at=run.completed_at,
                    )
                    session.add(model)
                    await session.flush()
                    return self._replay_ab_from_model(model)
        except IntegrityError:
            existing = await self.get_replay_ab(run.tenant_id, run.run_id)
            if existing is None:
                raise
            return existing

    @staticmethod
    def _from_model(model: SkillBaselineRunModel) -> BaselineRun:
        results = tuple(
            BaselineCaseResult(
                case_id=str(item.get("case_id") or ""),
                category=str(item.get("category") or ""),
                task_status=str(item.get("task_status") or ""),
                oracle_passed=bool(item.get("oracle_passed")),
                oracle_checks={
                    str(key): bool(value)
                    for key, value in dict(item.get("oracle_checks") or {}).items()
                },
                tick_count=int(item.get("tick_count") or 0),
                tool_call_count=int(item.get("tool_call_count") or 0),
                successful_tool_calls=tuple(
                    str(value) for value in item.get("successful_tool_calls", [])
                ),
                rejected_tool_calls=tuple(
                    {
                        "tool_name": str(value.get("tool_name") or ""),
                        "reason_code": str(value.get("reason_code") or ""),
                    }
                    for value in item.get("rejected_tool_calls", [])
                    if isinstance(value, dict)
                ),
                evidence_ids=tuple(
                    str(value) for value in item.get("evidence_ids", [])
                ),
                usage={
                    str(key): int(value)
                    for key, value in dict(item.get("usage") or {}).items()
                },
                latency_ms=float(item.get("latency_ms") or 0.0),
            )
            for item in (model.results_json or [])
        )
        return BaselineRun(
            run_id=model.id,
            tenant_id=model.tenant_id,
            schema_version=model.schema_version,
            case_definition_digest=model.case_definition_digest,
            runner=model.runner,
            candidate_loaded=model.candidate_loaded,
            results=results,
            started_at=_utc(model.started_at),
            completed_at=_utc(model.completed_at),
        )

    @staticmethod
    def _replay_ab_from_model(model: SkillReplayABRunModel) -> ReplayABRun:
        comparisons = tuple(
            ReplayABCaseComparison(
                case_id=str(item.get("case_id") or ""),
                category=str(item.get("category") or ""),
                baseline=_group_metrics(item.get("baseline"), "baseline"),
                candidate=_group_metrics(item.get("candidate"), "candidate"),
                deltas={
                    str(key): float(value)
                    for key, value in dict(item.get("deltas") or {}).items()
                },
            )
            for item in (model.comparisons_json or [])
        )
        aggregate = {
            str(section): {
                str(key): float(value) for key, value in dict(values).items()
            }
            for section, values in dict(model.aggregate_json or {}).items()
            if isinstance(values, dict)
        }
        return ReplayABRun(
            run_id=model.id,
            tenant_id=model.tenant_id,
            candidate_id=model.candidate_id,
            candidate_digest=model.candidate_digest,
            validation_report_id=model.validation_report_id,
            schema_version=model.schema_version,
            case_definition_digest=model.case_definition_digest,
            runner=model.runner,
            status=model.status,
            comparisons=comparisons,
            aggregate=aggregate,
            gate_checks={
                str(key): bool(value)
                for key, value in dict(model.gate_checks_json or {}).items()
            },
            gate_passed=model.gate_passed,
            failure_reason=model.failure_reason,
            started_at=_utc(model.started_at),
            completed_at=_utc(model.completed_at),
        )


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _group_metrics(value: object, group: str) -> ReplayGroupMetrics:
    raw = dict(value) if isinstance(value, dict) else {}
    return ReplayGroupMetrics(
        group="baseline" if group == "baseline" else "candidate",
        task_status=str(raw.get("task_status") or "failed"),
        task_success=bool(raw.get("task_success")),
        oracle_passed=bool(raw.get("oracle_passed")),
        root_cause_accurate=bool(raw.get("root_cause_accurate")),
        evidence_retention=float(raw.get("evidence_retention") or 0.0),
        answer_structure_complete=bool(raw.get("answer_structure_complete")),
        tick_count=int(raw.get("tick_count") or 0),
        tool_call_count=int(raw.get("tool_call_count") or 0),
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        total_tokens=int(raw.get("total_tokens") or 0),
        latency_ms=float(raw.get("latency_ms") or 0.0),
        retry_count=int(raw.get("retry_count") or 0),
        safety_violations=int(raw.get("safety_violations") or 0),
        illegal_tool_attempts=int(raw.get("illegal_tool_attempts") or 0),
        illegal_tool_executions=int(raw.get("illegal_tool_executions") or 0),
        unauthorized_access_attempts=int(raw.get("unauthorized_access_attempts") or 0),
        unauthorized_access_successes=int(
            raw.get("unauthorized_access_successes") or 0
        ),
        high_risk_action_attempts=int(raw.get("high_risk_action_attempts") or 0),
        high_risk_action_successes=int(raw.get("high_risk_action_successes") or 0),
        injection_attempts=int(raw.get("injection_attempts") or 0),
        injection_successes=int(raw.get("injection_successes") or 0),
        secret_leak_count=int(raw.get("secret_leak_count") or 0),
        timed_out=bool(raw.get("timed_out")),
        rollback_required=bool(raw.get("rollback_required")),
        rollback_passed=bool(raw.get("rollback_passed", True)),
        human_intervention_count=int(raw.get("human_intervention_count") or 0),
        repeat_count=int(raw.get("repeat_count") or 1),
        repeat_consistent=bool(raw.get("repeat_consistent")),
        failure_reason=(
            str(raw["failure_reason"]) if raw.get("failure_reason") else None
        ),
        candidate_loaded=bool(raw.get("candidate_loaded")),
        candidate_read_count=int(raw.get("candidate_read_count") or 0),
        candidate_skill_id=(
            str(raw["candidate_skill_id"]) if raw.get("candidate_skill_id") else None
        ),
        successful_tool_calls=tuple(
            str(item) for item in raw.get("successful_tool_calls", [])
        ),
        rejected_tool_calls=tuple(
            {
                "tool_name": str(item.get("tool_name") or ""),
                "reason_code": str(item.get("reason_code") or ""),
            }
            for item in raw.get("rejected_tool_calls", [])
            if isinstance(item, dict)
        ),
        latency_samples_ms=tuple(
            float(item) for item in raw.get("latency_samples_ms", [])
        ),
        execution_digests=tuple(str(item) for item in raw.get("execution_digests", [])),
        candidate_load_audit=dict(raw.get("candidate_load_audit") or {}),
    )


__all__ = ["SkillEvaluationRepository"]
