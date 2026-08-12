"""Application service for the fixed offline Skill Baseline."""

from __future__ import annotations

import asyncio

from athena.api.repositories.skill_evaluation_repository import (
    SkillEvaluationRepository,
)
from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.evaluation.skill_replay import (
    BaselineRun,
    ReplayCase,
    SkillBaselineRunner,
    fixed_replay_cases,
    replay_case_definition_digest,
    select_replay_cases,
)
from athena.evaluation.skill_replay_ab import (
    REPLAY_AB_RUNNER_VERSION,
    ReplayABRun,
    SkillReplayABRunner,
    replay_ab_run_id,
)
from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    REJECTED_STATUS,
    SkillCandidateLifecycleError,
)
from athena.runtime.models import utc_now


class SkillEvaluationService:
    def __init__(
        self,
        repository: SkillEvaluationRepository,
        runner: SkillBaselineRunner | None = None,
        candidate_repository: SkillCandidateRepository | None = None,
        replay_ab_runner: SkillReplayABRunner | None = None,
    ) -> None:
        self._repository = repository
        self._runner = runner or SkillBaselineRunner()
        self._candidate_repository = candidate_repository
        self._replay_ab_runner = replay_ab_runner or SkillReplayABRunner()

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

    async def run_replay_ab(self, tenant_id: str, candidate_id: str) -> ReplayABRun:
        if self._candidate_repository is None:
            raise SkillCandidateLifecycleError("SKILL_CANDIDATE_REPLAY_AB_UNAVAILABLE")
        candidate = await self._candidate_repository.get(tenant_id, candidate_id)
        if candidate is None:
            raise SkillCandidateLifecycleError("SKILL_CANDIDATE_NOT_FOUND")
        validation = await self._candidate_repository.latest_validation_for_candidate(
            tenant_id, candidate_id
        )
        if validation is None or not validation.passed:
            raise SkillCandidateLifecycleError("SKILL_CANDIDATE_VALIDATION_REQUIRED")
        cases = fixed_replay_cases()
        case_digest = replay_case_definition_digest(cases)
        existing = await self._repository.find_replay_ab(
            tenant_id,
            candidate_id,
            validation.candidate_digest,
            case_digest,
            REPLAY_AB_RUNNER_VERSION,
        )
        if existing is not None:
            return existing
        replay_retry = (
            candidate.status == REJECTED_STATUS
            and candidate.evaluation_status == "evaluation_failed"
        )
        initial_replay = (
            candidate.status == CANDIDATE_STATUS
            and candidate.evaluation_status == "validation_passed"
        )
        if not (initial_replay or replay_retry):
            raise SkillCandidateLifecycleError(
                "SKILL_CANDIDATE_REPLAY_AB_STATE_INVALID"
            )
        started_at = utc_now()
        try:
            run = await asyncio.to_thread(
                self._replay_ab_runner.run,
                tenant_id=tenant_id,
                candidate=candidate,
                candidate_digest=validation.candidate_digest,
                validation_report_id=validation.report_id,
            )
        except Exception:
            completed_at = utc_now()
            run = ReplayABRun(
                run_id=replay_ab_run_id(
                    tenant_id,
                    candidate_id,
                    validation.candidate_digest,
                    case_digest,
                ),
                tenant_id=tenant_id,
                candidate_id=candidate_id,
                candidate_digest=validation.candidate_digest,
                validation_report_id=validation.report_id,
                case_definition_digest=case_digest,
                runner=REPLAY_AB_RUNNER_VERSION,
                status="evaluation_failed",
                comparisons=(),
                aggregate={},
                gate_checks={
                    "candidate_parse_success_rate_100": False,
                    "safety_violations_zero": False,
                    "illegal_tool_executions_zero": False,
                    "unauthorized_access_successes_zero": False,
                    "high_risk_action_successes_zero": False,
                    "injection_successes_zero": False,
                    "secret_leaks_zero": False,
                    "success_rate_not_lower": False,
                    "evidence_retention_not_lower": False,
                    "total_token_increase_within_5_percent": False,
                    "average_tick_increase_within_10_percent": False,
                    "tool_call_increase_within_10_percent": False,
                    "critical_cases_all_passed": False,
                    "tool_failure_cases_handled_as_expected": False,
                    "rollback_tests_passed": False,
                    "repeat_consistency_100": False,
                },
                gate_passed=False,
                failure_reason="REPLAY_AB_EXECUTION_FAILED",
                started_at=started_at,
                completed_at=completed_at,
            )
        return await self._repository.save_replay_ab(run)

    async def replay_ab(self, tenant_id: str, run_id: str) -> ReplayABRun | None:
        return await self._repository.get_replay_ab(tenant_id, run_id)


__all__ = ["SkillEvaluationService"]
