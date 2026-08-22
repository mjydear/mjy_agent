"""An in-memory durable-store substitute for offline Runtime demonstrations."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any
from uuid import uuid4

from .models import (
    AgentTask,
    Artifact,
    ContextSnapshot,
    Event,
    Evidence,
    RuntimeSnapshot,
    TaskStatus,
    Tick,
    ToolEffectRecord,
    Usage,
    WorkingState,
    utc_now,
)


class TaskNotFoundError(KeyError):
    pass


class LeaseConflictError(RuntimeError):
    pass


@dataclass
class _Record:
    task: AgentTask
    lease_id: str | None = None
    ticks: list[Tick] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    artifacts: list[Artifact] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)
    working_state: WorkingState = field(default_factory=WorkingState)
    context: ContextSnapshot | None = None
    effects: dict[str, ToolEffectRecord] = field(default_factory=dict)


class InMemoryRuntimeStore:
    """Task aggregate storage with single-process lease ownership."""

    def __init__(self) -> None:
        self._records: dict[str, _Record] = {}

    def create_task(self, task: AgentTask) -> AgentTask:
        if task.task_id in self._records:
            raise ValueError(f"task already exists: {task.task_id}")
        record = _Record(task=task)
        self._records[task.task_id] = record
        self._append_event(
            record,
            tick_id=None,
            kind="task.created",
            payload={"goal": task.goal, "repository_root": task.repository_root},
        )
        return task

    def cancel_task(self, task_id: str) -> AgentTask:
        """Request cancellation; Runtime observes it at the next action boundary."""

        record = self._record(task_id)
        if not record.task.status.terminal:
            if record.task.status in {TaskStatus.QUEUED, TaskStatus.WAITING_HUMAN}:
                record.task.status = TaskStatus.CANCELLED
                event_kind = "task.cancelled"
            else:
                record.task.cancellation_requested = True
                event_kind = "task.cancel_requested"
            record.task.updated_at = utc_now()
            self._append_event(record, tick_id=None, kind=event_kind, payload={})
        return replace(record.task)

    def list_tasks(self) -> tuple[AgentTask, ...]:
        """Return task summaries without exposing mutable aggregate records."""

        return tuple(
            replace(record.task)
            for record in sorted(
                self._records.values(),
                key=lambda item: item.task.updated_at,
                reverse=True,
            )
        )

    def supply_human_input(self, task_id: str, value: str) -> AgentTask:
        """Persist operator input and re-queue a waiting task for one Tick."""

        if not value.strip():
            raise ValueError("human input must be a non-empty string")
        record = self._record(task_id)
        if record.task.status.value != "waiting_human":
            raise ValueError("human input is only accepted for waiting tasks")
        record.working_state = replace(record.working_state, human_input=value.strip())
        record.task.status = record.task.status.QUEUED
        record.task.updated_at = utc_now()
        self._append_event(
            record,
            tick_id=None,
            kind="task.resumed",
            payload={"input_received": True},
        )
        return replace(record.task)

    def claim(self, task_id: str, lease_id: str) -> AgentTask:
        if not lease_id.strip():
            raise ValueError("lease_id must be a non-empty string")
        record = self._record(task_id)
        if record.lease_id is not None and record.lease_id != lease_id:
            raise LeaseConflictError(f"task is leased by {record.lease_id}")
        record.lease_id = lease_id
        return record.task

    def snapshot(self, task_id: str) -> RuntimeSnapshot:
        record = self._record(task_id)
        return RuntimeSnapshot(
            task=replace(record.task),
            ticks=tuple(record.ticks),
            events=tuple(record.events),
            evidence=tuple(record.evidence),
            artifacts=tuple(record.artifacts),
            usage=tuple(record.usage),
            working_state=record.working_state,
            context=record.context,
        )

    def commit_tick(
        self,
        *,
        task_id: str,
        lease_id: str,
        task: AgentTask,
        tick: Tick,
        events: tuple[Event, ...],
        artifacts: tuple[Artifact, ...] = (),
        evidence: tuple[Evidence, ...] = (),
        usage: Usage,
        working_state: WorkingState,
        context: ContextSnapshot,
    ) -> None:
        record = self._record(task_id)
        if record.lease_id != lease_id:
            raise LeaseConflictError("lease must be claimed before committing")
        record.task = task
        record.ticks.append(tick)
        for event in events:
            self._append_event(
                record,
                tick_id=event.tick_id,
                kind=event.kind,
                payload=event.payload,
            )
        record.artifacts.extend(artifacts)
        record.evidence.extend(evidence)
        record.usage.append(usage)
        record.working_state = working_state
        record.context = context

    def persist_task(
        self,
        *,
        task_id: str,
        lease_id: str,
        task: AgentTask,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist a terminal action-boundary state that does not create a Tick."""

        record = self._record(task_id)
        if record.lease_id != lease_id:
            raise LeaseConflictError("lease must be claimed before persisting")
        record.task = task
        self._append_event(record, tick_id=None, kind=kind, payload=payload)

    def reserve_tool_effect(
        self, *, task_id: str, lease_id: str, effect_id: str, tool_name: str
    ) -> ToolEffectRecord:
        record = self._record(task_id)
        if record.lease_id != lease_id:
            raise LeaseConflictError("lease must be claimed before reserving an effect")
        existing = record.effects.get(effect_id)
        if existing is not None:
            return existing
        effect = ToolEffectRecord(
            effect_id=effect_id,
            task_id=task_id,
            tool_name=tool_name,
            status="reserved",
        )
        record.effects[effect_id] = effect
        return effect

    def complete_tool_effect(
        self, *, task_id: str, lease_id: str, effect: ToolEffectRecord
    ) -> None:
        record = self._record(task_id)
        if record.lease_id != lease_id:
            raise LeaseConflictError(
                "lease must be claimed before completing an effect"
            )
        if effect.effect_id not in record.effects:
            raise ValueError("effect must be reserved before completion")
        record.effects[effect.effect_id] = effect

    def effect_snapshot(self, task_id: str) -> tuple[ToolEffectRecord, ...]:
        """Return an immutable view for isolation and idempotency audits."""

        record = self._record(task_id)
        return tuple(record.effects.values())

    @staticmethod
    def _append_event(
        record: _Record,
        *,
        tick_id: str | None,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        record.events.append(
            Event(
                event_id=f"event_{uuid4().hex}",
                task_id=record.task.task_id,
                tick_id=tick_id,
                sequence=len(record.events) + 1,
                kind=kind,
                payload=payload,
                created_at=utc_now(),
            )
        )

    def _record(self, task_id: str) -> _Record:
        try:
            return self._records[task_id]
        except KeyError as exc:
            raise TaskNotFoundError(task_id) from exc
