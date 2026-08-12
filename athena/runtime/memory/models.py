"""Value objects for the Runtime's governed four-layer memory projection."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from athena.runtime.models import WorkingState


class SkillEvaluationState(StrEnum):
    """The only Skill states visible to Runtime memory retrieval."""

    CANDIDATE = "candidate"
    REPLAY_PENDING = "replay_pending"
    SHADOW_PENDING = "shadow_pending"
    REVIEW_PENDING = "review_pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True)
class PendingToolPair:
    """An unresolved tool effect that must survive history compression."""

    call_id: str
    tool_name: str
    request_summary: str
    result_summary: str | None = None

    def __post_init__(self) -> None:
        for name, value in (
            ("call_id", self.call_id),
            ("tool_name", self.tool_name),
            ("request_summary", self.request_summary),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

    def to_prompt_payload(
        self, *, text_limit: int | None = None
    ) -> dict[str, str | None]:
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "request_summary": _truncate(self.request_summary, text_limit),
            "result_summary": _truncate(self.result_summary, text_limit),
        }


@dataclass(frozen=True)
class RunningSummary:
    """Compressed, structured history rather than a model transcript."""

    completed_facts: tuple[str, ...] = ()
    failed_attempts: tuple[str, ...] = ()
    open_questions: tuple[str, ...] = ()
    next_actions: tuple[str, ...] = ()

    def to_prompt_payload(self) -> dict[str, list[str]]:
        return {
            "completed_facts": list(self.completed_facts),
            "failed_attempts": list(self.failed_attempts),
            "open_questions": list(self.open_questions),
            "next_actions": list(self.next_actions),
        }

    def compact(self, *, max_items: int, text_limit: int) -> "RunningSummary":
        if max_items <= 0 or text_limit <= 0:
            raise ValueError("summary compaction limits must be positive")
        return RunningSummary(
            completed_facts=_compact_items(
                self.completed_facts, max_items=max_items, text_limit=text_limit
            ),
            failed_attempts=_compact_items(
                self.failed_attempts, max_items=max_items, text_limit=text_limit
            ),
            open_questions=_compact_items(
                self.open_questions, max_items=max_items, text_limit=text_limit
            ),
            next_actions=_compact_items(
                self.next_actions, max_items=max_items, text_limit=text_limit
            ),
        )


@dataclass(frozen=True)
class MemoryCheckpoint:
    """Durable working-memory state supplied by the execution plane."""

    tick_sequence: int = 0
    working_state: WorkingState = field(default_factory=WorkingState)
    constraints: tuple[str, ...] = ()
    running_summary: RunningSummary = field(default_factory=RunningSummary)
    unresolved_tool_pairs: tuple[PendingToolPair, ...] = ()

    def __post_init__(self) -> None:
        if (
            isinstance(self.tick_sequence, bool)
            or not isinstance(self.tick_sequence, int)
            or self.tick_sequence < 0
        ):
            raise ValueError("tick_sequence must be a non-negative integer")

    @classmethod
    def from_working_state(
        cls, working_state: WorkingState, *, tick_sequence: int = 0
    ) -> "MemoryCheckpoint":
        """Adapt the P0 checkpoint while V1 durable checkpoints are introduced."""

        summary = (
            RunningSummary(completed_facts=(working_state.running_summary,))
            if working_state.running_summary
            else RunningSummary()
        )
        return cls(
            tick_sequence=tick_sequence,
            working_state=working_state,
            running_summary=summary,
        )


@dataclass(frozen=True)
class MemoryBudget:
    """Input capacity after reserving output and a safety margin first."""

    model_window_tokens: int
    output_reserve_tokens: int
    safety_margin_tokens: int = 0

    def __post_init__(self) -> None:
        for name, value in (
            ("model_window_tokens", self.model_window_tokens),
            ("output_reserve_tokens", self.output_reserve_tokens),
            ("safety_margin_tokens", self.safety_margin_tokens),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.model_window_tokens <= 0:
            raise ValueError("model_window_tokens must be positive")

    @property
    def input_capacity_tokens(self) -> int:
        return max(
            0,
            self.model_window_tokens
            - self.output_reserve_tokens
            - self.safety_margin_tokens,
        )


class MemoryBudgetError(ValueError):
    """The mandatory, non-droppable memory anchors cannot fit the budget."""

    def __init__(self, *, required_tokens: int, available_tokens: int) -> None:
        self.required_tokens = required_tokens
        self.available_tokens = available_tokens
        super().__init__(
            "runtime memory anchors exceed the available input budget "
            f"(required={required_tokens}, available={available_tokens})"
        )


def _compact_items(
    items: tuple[str, ...], *, max_items: int, text_limit: int
) -> tuple[str, ...]:
    # The latest entries are most relevant for the next ReAct decision.
    return tuple(_truncate(item, text_limit) or "" for item in items[-max_items:])


def _truncate(value: str | None, text_limit: int | None) -> str | None:
    if value is None or text_limit is None or len(value) <= text_limit:
        return value
    if text_limit == 1:
        return value[:1]
    return value[: text_limit - 1].rstrip() + "..."
