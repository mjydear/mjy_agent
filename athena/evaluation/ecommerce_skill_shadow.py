"""Candidate Shadow execution for the ecommerce backend slice."""

from __future__ import annotations

from pathlib import Path

from athena.evaluation.backend_replay import (
    EcommerceDiagnosisCase,
    EcommerceReplayToolCatalog,
    fixed_ecommerce_diagnosis_cases,
)
from athena.evaluation.ecommerce_skill_replay_ab import ecommerce_skill_replay_cases
from athena.evaluation.skill_replay import ReplayCase
from athena.evaluation.skill_shadow import SkillShadowRunner

ECOMMERCE_SHADOW_RUNNER_VERSION = "ecommerce-agent-runtime-shadow-v1"


class EcommerceSkillShadowRunner(SkillShadowRunner):
    """Run the existing Shadow protocol with ecommerce tools and Case oracles."""

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
        return ECOMMERCE_SHADOW_RUNNER_VERSION


__all__ = [
    "ECOMMERCE_SHADOW_RUNNER_VERSION",
    "EcommerceSkillShadowRunner",
]
