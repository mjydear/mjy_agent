"""Persistable OpsTask state and deterministic transition validation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum

from athena.agent.policy.contracts import (
    ActionDecision,
    EnvironmentMode,
    ExecutionProfile,
)
from athena.types import JSONValue


class OpsTaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING = "waiting"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OpsTaskPhase(StrEnum):
    VALIDATE = "validate"
    COLLECT = "collect"
    ANALYZE = "analyze"
    PLAN = "plan"
    APPROVE = "approve"
    EXECUTE = "execute"
    VERIFY = "verify"
    REPORT = "report"


_ALLOWED_STATUS_TRANSITIONS: dict[OpsTaskStatus, frozenset[OpsTaskStatus]] = {
    OpsTaskStatus.QUEUED: frozenset({OpsTaskStatus.RUNNING, OpsTaskStatus.CANCELLED}),
    OpsTaskStatus.RUNNING: frozenset(
        {
            OpsTaskStatus.WAITING,
            OpsTaskStatus.SUCCEEDED,
            OpsTaskStatus.FAILED,
            OpsTaskStatus.CANCELLED,
        }
    ),
    OpsTaskStatus.WAITING: frozenset(
        {OpsTaskStatus.RUNNING, OpsTaskStatus.FAILED, OpsTaskStatus.CANCELLED}
    ),
    OpsTaskStatus.SUCCEEDED: frozenset(),
    OpsTaskStatus.FAILED: frozenset(),
    OpsTaskStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True)
class TaskBudget:
    remaining_steps: int
    remaining_tokens: int
    remaining_time_ms: int

    def __post_init__(self) -> None:
        if min(self.remaining_steps, self.remaining_tokens, self.remaining_time_ms) < 0:
            raise ValueError("task budget values must be non-negative")


@dataclass(frozen=True)
class OpsTaskState:
    task_id: str
    tenant_id: str
    objective: str
    environment_id: str
    environment_mode: EnvironmentMode
    scope: dict[str, JSONValue]
    tenant_policy_snapshot: dict[str, JSONValue]
    budget: TaskBudget
    execution_profile: ExecutionProfile
    status: OpsTaskStatus = OpsTaskStatus.QUEUED
    phase: OpsTaskPhase = OpsTaskPhase.VALIDATE
    facts: tuple[dict[str, JSONValue], ...] = ()
    hypotheses: tuple[dict[str, JSONValue], ...] = ()
    completed_actions: tuple[ActionDecision, ...] = ()
    failed_actions: tuple[ActionDecision, ...] = ()
    action_history: tuple[ActionDecision, ...] = ()
    skill_version_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    state_version: int = 0

    def __post_init__(self) -> None:
        for field_name, value in (
            ("task_id", self.task_id),
            ("tenant_id", self.tenant_id),
            ("objective", self.objective),
            ("environment_id", self.environment_id),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.state_version < 0:
            raise ValueError("state_version must be non-negative")
        if (self.lease_owner is None) != (self.lease_expires_at is None):
            raise ValueError("lease owner and expiry must be set together")
        if self.lease_expires_at is not None and self.lease_expires_at.tzinfo is None:
            raise ValueError("lease expiry must be timezone-aware")

    def transition_to(
        self, status: OpsTaskStatus, phase: OpsTaskPhase | None = None
    ) -> "OpsTaskState":
        if status not in _ALLOWED_STATUS_TRANSITIONS[self.status]:
            raise ValueError(f"invalid task transition: {self.status} -> {status}")
        return replace(
            self,
            status=status,
            phase=phase or self.phase,
            state_version=self.state_version + 1,
        )
