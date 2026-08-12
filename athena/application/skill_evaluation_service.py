"""Application service for the fixed offline Skill Baseline."""

from __future__ import annotations

import asyncio

from athena.api.repositories.skill_evaluation_repository import (
    SkillEvaluationRepository,
)
from athena.evaluation.skill_replay import (
    BaselineRun,
    ReplayCase,
    SkillBaselineRunner,
    fixed_replay_cases,
    select_replay_cases,
)


class SkillEvaluationService:
    def __init__(
        self,
        repository: SkillEvaluationRepository,
        runner: SkillBaselineRunner | None = None,
    ) -> None:
        self._repository = repository
        self._runner = runner or SkillBaselineRunner()

    def cases(self) -> tuple[ReplayCase, ...]:
        return fixed_replay_cases()

    async def run_baseline(
        self, tenant_id: str, *, case_ids: tuple[str, ...] = ()
    ) -> BaselineRun:
        cases = select_replay_cases(case_ids)
        run = await asyncio.to_thread(
            self._runner.run,
            tenant_id=tenant_id,
            cases=cases,
        )
        return await self._repository.save_baseline(run)

    async def baseline(self, tenant_id: str, run_id: str) -> BaselineRun | None:
        return await self._repository.get_baseline(tenant_id, run_id)


__all__ = ["SkillEvaluationService"]
