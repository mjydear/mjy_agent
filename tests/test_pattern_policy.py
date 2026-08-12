"""Tests for deterministic Phase 0 execution profile selection."""

from __future__ import annotations

import pytest

from athena.agent.policy.contracts import ExecutionProfile, RiskLevel
from athena.agent.policy.pattern_policy import PatternPolicy, PatternPolicyInput
from athena.config import AthenaSettings


def _request(**overrides: object) -> PatternPolicyInput:
    values: dict[str, object] = {
        "task_type": "crashloop_diagnosis",
        "risk_level": RiskLevel.S1,
        "required_capabilities": ("k8s.logs.read",),
        "estimated_steps": 3,
        "evidence_fanout": 1,
        "remaining_tokens": 6000,
        "remaining_time_ms": 30000,
    }
    values.update(overrides)
    return PatternPolicyInput(**values)  # type: ignore[arg-type]


def test_pattern_policy_selects_minimum_deterministic_profile() -> None:
    policy = PatternPolicy()
    simple = policy.select(_request(task_type="read_query", estimated_steps=1))
    assert simple.profile is ExecutionProfile.DIRECT_WORKFLOW

    diagnostic = policy.select(_request())
    assert diagnostic.profile is ExecutionProfile.BOUNDED_POLICY_LOOP

    planned = policy.select(_request(estimated_steps=4, evidence_fanout=2))
    assert planned.profile is ExecutionProfile.PLAN_EXECUTE


def test_modifiers_are_constrained_by_risk_and_confidence() -> None:
    selected = PatternPolicy().select(
        _request(evidence_fanout=2, current_confidence=0.2)
    )
    assert selected.modifiers == ("parallel_read_collection", "max_one_reflection")

    assert (
        "parallel_read_collection"
        not in PatternPolicy()
        .select(_request(risk_level=RiskLevel.S3, evidence_fanout=3))
        .modifiers
    )


def test_prohibited_profiles_are_not_selected() -> None:
    with pytest.raises(ValueError):
        PatternPolicy().select(_request(risk_level=RiskLevel.S5))


def test_execution_mode_defaults_to_legacy_react() -> None:
    assert AthenaSettings().agent.execution_mode == "legacy_react"
