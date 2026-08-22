"""Persist safe ecommerce Runtime traces for the Skill learning pipeline."""

from __future__ import annotations

import asyncio

from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.evaluation.backend_replay import (
    EcommerceDiagnosisCase,
    EcommerceDiagnosisCaseRepository,
    execute_ecommerce_replay_case,
)
from athena.runtime.learning import TrajectorySummary, TrajectorySummaryBuilder


class EcommerceSkillTraceService:
    """Convert observed ecommerce Runtime executions into durable trajectories."""

    def __init__(
        self,
        repository: SkillCandidateRepository,
        *,
        cases: tuple[EcommerceDiagnosisCase, ...] | None = None,
    ) -> None:
        self._repository = repository
        self._cases = EcommerceDiagnosisCaseRepository(cases)
        self._trajectory_builder = TrajectorySummaryBuilder()

    async def capture(
        self,
        *,
        tenant_id: str,
        case_id: str,
    ) -> dict[str, object]:
        case = self._cases.get(case_id)
        evaluation, snapshot = await asyncio.to_thread(
            execute_ecommerce_replay_case, case
        )
        trajectory = self._trajectory_builder.build(snapshot, tenant_id=tenant_id)
        trajectory = await self._repository.save_trajectory(trajectory)
        return {
            "case_id": case.case_id,
            "evaluation": evaluation.to_dict(),
            "trajectory": trajectory.to_dict(),
            "candidate_generation": {
                "eligible": trajectory.admission.eligible,
                "next_endpoint": "/api/skill-candidates/generations",
                "source_trajectory_ids": [trajectory.trajectory_id],
            },
        }

    async def get_trajectory(
        self, *, tenant_id: str, trajectory_id: str
    ) -> TrajectorySummary | None:
        return await self._repository.get_trajectory(tenant_id, trajectory_id)


__all__ = ["EcommerceSkillTraceService"]
