"""Deterministic execution-profile selection for OpsTask workflows."""

from __future__ import annotations

from dataclasses import dataclass

from athena.agent.policy.contracts import ExecutionProfile, RiskLevel


@dataclass(frozen=True)
class PatternPolicyInput:
    task_type: str
    risk_level: RiskLevel
    required_capabilities: tuple[str, ...]
    estimated_steps: int
    evidence_fanout: int
    remaining_tokens: int
    remaining_time_ms: int
    current_confidence: float | None = None

    def __post_init__(self) -> None:
        if not self.task_type.strip():
            raise ValueError("task_type must be a non-empty string")
        if (
            min(
                self.estimated_steps,
                self.evidence_fanout,
                self.remaining_tokens,
                self.remaining_time_ms,
            )
            < 0
        ):
            raise ValueError("policy input budgets must be non-negative")
        if (
            self.current_confidence is not None
            and not 0 <= self.current_confidence <= 1
        ):
            raise ValueError("current_confidence must be between 0 and 1")


@dataclass(frozen=True)
class PatternSelection:
    profile: ExecutionProfile
    modifiers: tuple[str, ...]


class PatternPolicy:
    """Select the smallest allowed profile without involving a model."""

    _SIMPLE_TASK_TYPES = frozenset({"knowledge", "read_query", "health_check"})

    def select(self, request: PatternPolicyInput) -> PatternSelection:
        if request.risk_level in {RiskLevel.S4, RiskLevel.S5}:
            raise ValueError(
                "PatternPolicy does not select disabled or prohibited write profiles"
            )

        modifiers: list[str] = []
        if request.evidence_fanout > 1 and request.risk_level in {
            RiskLevel.S0,
            RiskLevel.S1,
        }:
            modifiers.append("parallel_read_collection")
        if request.current_confidence is not None and request.current_confidence < 0.5:
            modifiers.append("max_one_reflection")

        if request.risk_level is RiskLevel.S3 or request.estimated_steps >= 4:
            return PatternSelection(ExecutionProfile.PLAN_EXECUTE, tuple(modifiers))
        if (
            request.task_type in self._SIMPLE_TASK_TYPES
            and request.estimated_steps <= 1
            and request.evidence_fanout <= 1
        ):
            return PatternSelection(ExecutionProfile.DIRECT_WORKFLOW, tuple(modifiers))
        return PatternSelection(ExecutionProfile.BOUNDED_POLICY_LOOP, tuple(modifiers))
