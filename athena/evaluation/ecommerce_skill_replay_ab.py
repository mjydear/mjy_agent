"""Candidate-vs-Baseline Replay A/B for the ecommerce backend slice."""

from __future__ import annotations

from pathlib import Path

from athena.evaluation.backend_replay import (
    EcommerceDiagnosisCase,
    EcommerceReplayToolCatalog,
    fixed_ecommerce_diagnosis_cases,
)
from athena.evaluation.skill_replay import (
    ReplayCase,
    ReplayCaseCategory,
    ReplaySuccessOracle,
    ReplayToolPolicy,
)
from athena.evaluation.skill_replay_ab import SkillReplayABRunner

ECOMMERCE_REPLAY_AB_RUNNER_VERSION = "ecommerce-agent-runtime-candidate-ab-v1"

ECOMMERCE_REPLAY_TOOL_NAMES = frozenset(
    call.tool_name
    for case in fixed_ecommerce_diagnosis_cases()
    for call in case.tool_call_plan
)


def ecommerce_skill_replay_cases(
    cases: tuple[EcommerceDiagnosisCase, ...] | None = None,
) -> tuple[ReplayCase, ...]:
    """Project fixed ecommerce oracles into the shared Skill Replay contract."""

    selected = cases or fixed_ecommerce_diagnosis_cases()
    return tuple(
        ReplayCase(
            case_id=case.case_id,
            category=_category(case),
            input=case.task_goal,
            fixture_id=case.fixture_id,
            fixture_files=(),
            tool_policy=ReplayToolPolicy(
                allowed_tools=case.safety_oracle.allowed_readonly_tool_names,
                forbidden_tools=case.safety_oracle.forbidden_tool_names,
                readonly_only=case.safety_oracle.require_readonly,
            ),
            required_evidence=case.success_oracle.required_tool_names,
            max_ticks=case.max_ticks,
            max_tool_calls=case.max_tool_calls,
            success_oracle=ReplaySuccessOracle(
                expected_task_status=case.success_oracle.expected_task_status.value,
                required_tool_names=case.success_oracle.required_tool_names,
                expected_rejection_code=case.success_oracle.expected_rejection_code,
            ),
            decisions=case.decisions,
        )
        for case in selected
    )


class EcommerceSkillReplayABRunner(SkillReplayABRunner):
    """Run the shared A/B evaluator with ecommerce fixtures and tool policy."""

    def __init__(
        self,
        cases: tuple[EcommerceDiagnosisCase, ...] | None = None,
    ) -> None:
        source_cases = cases or fixed_ecommerce_diagnosis_cases()
        replay_cases = ecommerce_skill_replay_cases(source_cases)
        case_by_id = {case.case_id: case for case in source_cases}

        def catalog_factory(case: ReplayCase):
            return EcommerceReplayToolCatalog(case_by_id[case.case_id])

        super().__init__(
            repository_root=Path(__file__).resolve().parents[2],
            cases=replay_cases,
            tool_catalog_factory=catalog_factory,
        )

    @property
    def runner_version(self) -> str:
        return ECOMMERCE_REPLAY_AB_RUNNER_VERSION


def _category(case: EcommerceDiagnosisCase) -> ReplayCaseCategory:
    if case.category.value == "security":
        return ReplayCaseCategory.SECURITY_REJECTION
    if case.category.value == "tool_failure":
        return ReplayCaseCategory.TOOL_FAILURE
    if len(case.tool_call_plan) == 1:
        return ReplayCaseCategory.SIMPLE
    return ReplayCaseCategory.MULTI_STEP


__all__ = [
    "ECOMMERCE_REPLAY_AB_RUNNER_VERSION",
    "ECOMMERCE_REPLAY_TOOL_NAMES",
    "EcommerceSkillReplayABRunner",
    "ecommerce_skill_replay_cases",
]
