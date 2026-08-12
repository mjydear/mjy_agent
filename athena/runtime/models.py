"""Domain objects for the durable, inspectable Agent Runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(UTC)


class TaskStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BUDGET_EXHAUSTED = "budget_exhausted"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.BUDGET_EXHAUSTED,
            TaskStatus.CANCELLED,
        }


class TaskProfile(StrEnum):
    SIMPLE = "simple"
    STANDARD = "standard"
    COMPLEX = "complex"


class DecisionKind(StrEnum):
    TOOL_CALL = "tool_call"
    FINAL = "final"
    ASK_HUMAN = "ask_human"
    FAIL = "fail"


class TickStatus(StrEnum):
    COMPLETED = "completed"
    WAITING_HUMAN = "waiting_human"
    FAILED = "failed"


@dataclass(frozen=True)
class TaskBudget:
    total_tokens: int
    max_ticks: int
    output_reserve_tokens: int = 512
    consumed_tokens: int = 0

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.total_tokens - self.consumed_tokens)

    @property
    def mode(self) -> str:
        if self.total_tokens <= 0:
            return "EXHAUSTED"
        consumed_ratio = self.consumed_tokens / self.total_tokens
        if consumed_ratio < 0.70:
            return "NORMAL"
        if consumed_ratio < 0.85:
            return "ECONOMY"
        if consumed_ratio < 0.95:
            return "CONVERGE"
        if consumed_ratio < 1.0:
            return "FINALIZE"
        return "EXHAUSTED"

    def consume(self, tokens: int) -> "TaskBudget":
        if tokens < 0:
            raise ValueError("tokens must be non-negative")
        return TaskBudget(
            total_tokens=self.total_tokens,
            max_ticks=self.max_ticks,
            output_reserve_tokens=self.output_reserve_tokens,
            consumed_tokens=self.consumed_tokens + tokens,
        )


def profile_budget(profile: TaskProfile) -> TaskBudget:
    if profile is TaskProfile.SIMPLE:
        return TaskBudget(total_tokens=12_000, max_ticks=2)
    if profile is TaskProfile.COMPLEX:
        return TaskBudget(total_tokens=120_000, max_ticks=10)
    return TaskBudget(total_tokens=50_000, max_ticks=6)


@dataclass
class AgentTask:
    task_id: str
    goal: str
    repository_root: str
    profile: TaskProfile
    budget: TaskBudget
    status: TaskStatus = TaskStatus.QUEUED
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime = field(default_factory=utc_now)
    final_report: "FinalReport | None" = None
    cancellation_requested: bool = False

    @classmethod
    def create(
        cls,
        *,
        goal: str,
        repository_root: str,
        profile: TaskProfile = TaskProfile.STANDARD,
    ) -> "AgentTask":
        if not goal.strip():
            raise ValueError("goal must be a non-empty string")
        if not repository_root.strip():
            raise ValueError("repository_root must be a non-empty string")
        return cls(
            task_id=f"task_{uuid4().hex}",
            goal=goal.strip(),
            repository_root=repository_root,
            profile=profile,
            budget=profile_budget(profile),
        )


@dataclass(frozen=True)
class Decision:
    kind: DecisionKind
    reason_code: str
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    response: str | None = None

    def __post_init__(self) -> None:
        if not self.reason_code.strip():
            raise ValueError("reason_code must be a non-empty string")
        if self.kind is DecisionKind.TOOL_CALL and not self.tool_name:
            raise ValueError("tool_call decisions require tool_name")
        if self.kind is not DecisionKind.TOOL_CALL and self.tool_name is not None:
            raise ValueError("only tool_call decisions may include tool_name")
        if self.kind in {DecisionKind.FINAL, DecisionKind.ASK_HUMAN, DecisionKind.FAIL}:
            if not self.response or not self.response.strip():
                raise ValueError(f"{self.kind.value} decisions require response")

    def to_public_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "kind": self.kind.value,
            "reason_code": self.reason_code,
        }
        if self.tool_name is not None:
            payload["tool_name"] = self.tool_name
        if self.response is not None:
            payload["response"] = self.response
        return payload


@dataclass(frozen=True)
class FinalReport:
    root_cause: str
    repair_recommendation: str
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class Tick:
    tick_id: str | None
    task_id: str
    sequence: int
    decision: Decision
    status: TickStatus
    created_at: datetime


@dataclass(frozen=True)
class Event:
    event_id: str
    task_id: str
    tick_id: str
    sequence: int
    kind: str
    payload: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True)
class Artifact:
    artifact_id: str
    task_id: str
    tick_id: str
    tool_name: str
    content: dict[str, Any]
    content_hash: str
    created_at: datetime


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    task_id: str
    artifact_id: str
    source: str
    summary: str
    created_at: datetime


@dataclass(frozen=True)
class ToolEffectRecord:
    """Idempotency journal entry for one logical read-only tool action."""

    effect_id: str
    task_id: str
    tool_name: str
    status: str
    artifact: Artifact | None = None
    evidence: Evidence | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class Usage:
    usage_id: str
    task_id: str
    tick_id: str
    purpose: str
    model_tier: str
    route_reason: str
    estimated_input_tokens: int
    reserved_tokens: int
    actual_input_tokens: int
    actual_output_tokens: int
    budget_mode: str
    created_at: datetime

    @property
    def actual_tokens(self) -> int:
        return self.actual_input_tokens + self.actual_output_tokens


@dataclass(frozen=True)
class WorkingState:
    plan: tuple[str, ...] = ()
    pending_items: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    running_summary: str = ""
    human_input: str | None = None
    compaction_count: int = 0


@dataclass(frozen=True)
class ContextSnapshot:
    task_id: str
    tick_sequence: int
    payload: dict[str, Any]
    estimated_input_tokens: int
    input_budget_tokens: int
    output_reserve_tokens: int
    compacted: bool
    omitted_event_count: int
    compaction_count: int = 0
    # Tool schemas are server-selected metadata rather than model-authored
    # context. Keeping them out of the memory payload preserves the V1 memory
    # contract while letting the decision engine validate its visible tools.
    tool_schemas: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class RuntimeSnapshot:
    task: AgentTask
    ticks: tuple[Tick, ...]
    events: tuple[Event, ...]
    evidence: tuple[Evidence, ...]
    artifacts: tuple[Artifact, ...]
    usage: tuple[Usage, ...]
    working_state: WorkingState
    context: ContextSnapshot | None = None


@dataclass(frozen=True)
class AdvanceResult:
    task: AgentTask
    tick: Tick | None
    decision: Decision | None
    context: ContextSnapshot | None
