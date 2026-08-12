"""Tenant-scoped persistence for fixed Skill Baseline observations."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.evaluation.skill_replay import BaselineCaseResult, BaselineRun

from .models import SkillBaselineRunModel


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


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = ["SkillEvaluationRepository"]
