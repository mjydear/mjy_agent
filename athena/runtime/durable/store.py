"""Durable RuntimeStore implementation with lease fencing and aggregate commits."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Callable
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from athena.runtime.models import (
    AgentTask,
    Artifact,
    ContextSnapshot,
    Decision,
    DecisionKind,
    Event,
    Evidence,
    FinalReport,
    RuntimeSnapshot,
    TaskBudget,
    TaskProfile,
    TaskStatus,
    Tick,
    TickStatus,
    ToolEffectRecord,
    Usage,
    WorkingState,
)
from athena.runtime.store import LeaseConflictError, TaskNotFoundError

from .models import (
    RuntimeAgentTaskModel,
    RuntimeArtifactModel,
    RuntimeCheckpointModel,
    RuntimeEvidenceModel,
    RuntimeTickEventModel,
    RuntimeUsageModel,
    RuntimeToolEffectModel,
)


class DurableRuntimeStore:
    """Persist one Runtime aggregate per SQL transaction.

    The public methods intentionally match ``InMemoryRuntimeStore`` so the
    synchronous ``AgentRuntime`` can switch adapters without a second control
    flow. A lease id acts as a fencing token; an expired owner cannot commit a
    tick after another worker takes the task.
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        lease_seconds: float = 30.0,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        self._sessions = session_factory
        self._lease_duration = timedelta(seconds=lease_seconds)
        self._now = now or (lambda: datetime.now(UTC))

    def create_task(self, task: AgentTask) -> AgentTask:
        with self._sessions.begin() as session:
            if session.get(RuntimeAgentTaskModel, task.task_id) is not None:
                raise ValueError(f"task already exists: {task.task_id}")
            session.add(self._task_model(task))
            self._append_event(
                session,
                task_id=task.task_id,
                tick_id=None,
                kind="task.created",
                payload={"goal": task.goal, "repository_root": task.repository_root},
            )
        return replace(task)

    def list_tasks(self) -> tuple[AgentTask, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(RuntimeAgentTaskModel).order_by(
                    RuntimeAgentTaskModel.updated_at.desc()
                )
            ).all()
            return tuple(self._task_from_model(row) for row in rows)

    def cancel_task(self, task_id: str) -> AgentTask:
        with self._sessions.begin() as session:
            row = self._locked_task(session, task_id)
            task = self._task_from_model(row)
            if task.status.terminal:
                return task
            if task.status in {TaskStatus.QUEUED, TaskStatus.WAITING_HUMAN}:
                task.status = TaskStatus.CANCELLED
                event_kind = "task.cancelled"
            else:
                task.cancellation_requested = True
                event_kind = "task.cancel_requested"
            task.updated_at = self._timestamp()
            self._copy_task_to_model(task, row)
            self._append_event(
                session, task_id=task_id, tick_id=None, kind=event_kind, payload={}
            )
            return replace(task)

    def supply_human_input(self, task_id: str, value: str) -> AgentTask:
        if not value.strip():
            raise ValueError("human input must be a non-empty string")
        with self._sessions.begin() as session:
            row = self._locked_task(session, task_id)
            task = self._task_from_model(row)
            if task.status is not TaskStatus.WAITING_HUMAN:
                raise ValueError("human input is only accepted for waiting tasks")
            checkpoint = self._latest_checkpoint(session, task_id)
            state = self._working_state_from_json(
                checkpoint.working_state_json if checkpoint is not None else {}
            )
            state = replace(state, human_input=value.strip())
            task.status = TaskStatus.QUEUED
            task.updated_at = self._timestamp()
            self._copy_task_to_model(task, row)
            if checkpoint is None:
                session.add(
                    RuntimeCheckpointModel(
                        id=self._id("checkpoint"),
                        task_id=task_id,
                        checkpoint_version=row.checkpoint_version,
                        working_state_json=self._working_state_json(state),
                        context_json=None,
                        created_at=self._timestamp(),
                    )
                )
            else:
                # Human input changes resumable state, but does not consume a
                # ReAct Tick. Keep checkpoint_version aligned with Tick.sequence.
                checkpoint.working_state_json = self._working_state_json(state)
                checkpoint.created_at = self._timestamp()
            self._append_event(
                session,
                task_id=task_id,
                tick_id=None,
                kind="task.resumed",
                payload={"input_received": True},
            )
            return replace(task)

    def claim(self, task_id: str, lease_id: str) -> AgentTask:
        if not lease_id.strip():
            raise ValueError("lease_id must be a non-empty string")
        with self._sessions.begin() as session:
            row = self._locked_task(session, task_id)
            now = self._timestamp()
            held_by_other = row.lease_id is not None and row.lease_id != lease_id
            live_lease = (
                row.lease_expires_at is not None
                and self._as_utc(row.lease_expires_at) > now
            )
            if held_by_other and live_lease:
                raise LeaseConflictError(f"task is leased by {row.lease_id}")
            if row.lease_id != lease_id:
                row.lease_generation += 1
            row.lease_id = lease_id
            row.lease_expires_at = now + self._lease_duration
            return self._task_from_model(row)

    def snapshot(self, task_id: str) -> RuntimeSnapshot:
        with self._sessions() as session:
            task = self._task_from_model(self._task_or_raise(session, task_id))
            events = tuple(
                self._event_from_model(row)
                for row in session.scalars(
                    select(RuntimeTickEventModel)
                    .where(RuntimeTickEventModel.task_id == task_id)
                    .order_by(RuntimeTickEventModel.sequence)
                ).all()
            )
            completed = session.scalars(
                select(RuntimeTickEventModel)
                .where(
                    RuntimeTickEventModel.task_id == task_id,
                    RuntimeTickEventModel.kind == "tick.completed",
                )
                .order_by(
                    RuntimeTickEventModel.tick_sequence, RuntimeTickEventModel.sequence
                )
            ).all()
            checkpoint = self._latest_checkpoint(session, task_id)
            return RuntimeSnapshot(
                task=task,
                ticks=tuple(self._tick_from_event(row) for row in completed),
                events=events,
                evidence=tuple(
                    self._evidence_from_model(row)
                    for row in session.scalars(
                        select(RuntimeEvidenceModel)
                        .where(RuntimeEvidenceModel.task_id == task_id)
                        .order_by(RuntimeEvidenceModel.created_at)
                    ).all()
                ),
                artifacts=tuple(
                    self._artifact_from_model(row)
                    for row in session.scalars(
                        select(RuntimeArtifactModel)
                        .where(RuntimeArtifactModel.task_id == task_id)
                        .order_by(RuntimeArtifactModel.created_at)
                    ).all()
                ),
                usage=tuple(
                    self._usage_from_model(row)
                    for row in session.scalars(
                        select(RuntimeUsageModel)
                        .where(RuntimeUsageModel.task_id == task_id)
                        .order_by(RuntimeUsageModel.created_at)
                    ).all()
                ),
                working_state=self._working_state_from_json(
                    checkpoint.working_state_json if checkpoint is not None else {}
                ),
                context=(
                    self._context_from_json(checkpoint.context_json)
                    if checkpoint is not None and checkpoint.context_json is not None
                    else None
                ),
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
        """Atomically save one decision Tick and its complete observable result."""

        if (
            task.task_id != task_id
            or tick.task_id != task_id
            or usage.task_id != task_id
        ):
            raise ValueError("aggregate records must belong to the committed task")
        if any(item.task_id != task_id for item in (*artifacts, *evidence, *events)):
            raise ValueError("aggregate records must belong to the committed task")
        with self._sessions.begin() as session:
            row = self._require_live_lease(session, task_id, lease_id)
            if tick.sequence != row.checkpoint_version + 1:
                raise LeaseConflictError("tick sequence is stale or out of order")
            self._copy_task_to_model(task, row)
            row.checkpoint_version += 1
            for event in events:
                is_completed = event.kind == "tick.completed"
                self._append_event(
                    session,
                    task_id=task_id,
                    tick_id=event.tick_id,
                    kind=event.kind,
                    payload=event.payload,
                    event_id=event.event_id,
                    tick_sequence=tick.sequence if is_completed else None,
                    decision=tick.decision if is_completed else None,
                    tick_status=tick.status if is_completed else None,
                    created_at=event.created_at,
                )
            session.add_all(
                [
                    RuntimeArtifactModel(
                        id=item.artifact_id,
                        task_id=item.task_id,
                        tick_id=item.tick_id,
                        tool_name=item.tool_name,
                        content_json=self._json_value(item.content),
                        content_hash=item.content_hash,
                        created_at=item.created_at,
                    )
                    for item in artifacts
                ]
            )
            session.add_all(
                [
                    RuntimeEvidenceModel(
                        id=item.evidence_id,
                        task_id=item.task_id,
                        artifact_id=item.artifact_id,
                        source=item.source,
                        summary=item.summary,
                        created_at=item.created_at,
                    )
                    for item in evidence
                ]
            )
            session.add(
                RuntimeUsageModel(
                    id=usage.usage_id,
                    task_id=usage.task_id,
                    tick_id=usage.tick_id,
                    purpose=usage.purpose,
                    model_tier=usage.model_tier,
                    route_reason=usage.route_reason,
                    estimated_input_tokens=usage.estimated_input_tokens,
                    reserved_tokens=usage.reserved_tokens,
                    actual_input_tokens=usage.actual_input_tokens,
                    actual_output_tokens=usage.actual_output_tokens,
                    budget_mode=usage.budget_mode,
                    created_at=usage.created_at,
                )
            )
            session.add(
                RuntimeCheckpointModel(
                    id=self._id("checkpoint"),
                    task_id=task_id,
                    checkpoint_version=row.checkpoint_version,
                    working_state_json=self._working_state_json(working_state),
                    context_json=self._context_json(context),
                    created_at=self._timestamp(),
                )
            )

    def persist_task(
        self,
        *,
        task_id: str,
        lease_id: str,
        task: AgentTask,
        kind: str,
        payload: dict[str, Any],
    ) -> None:
        """Persist an action-boundary status change that does not create a Tick."""

        if task.task_id != task_id:
            raise ValueError("task must belong to the aggregate being persisted")
        with self._sessions.begin() as session:
            row = self._require_live_lease(session, task_id, lease_id)
            self._copy_task_to_model(task, row)
            self._append_event(
                session,
                task_id=task_id,
                tick_id=None,
                kind=kind,
                payload=payload,
            )

    def reserve_tool_effect(
        self, *, task_id: str, lease_id: str, effect_id: str, tool_name: str
    ) -> ToolEffectRecord:
        with self._sessions.begin() as session:
            self._require_live_lease(session, task_id, lease_id)
            row = session.scalar(
                select(RuntimeToolEffectModel).where(
                    RuntimeToolEffectModel.task_id == task_id,
                    RuntimeToolEffectModel.effect_id == effect_id,
                )
            )
            if row is None:
                row = RuntimeToolEffectModel(
                    id=self._id("effect"),
                    task_id=task_id,
                    effect_id=effect_id,
                    tool_name=tool_name,
                    status="reserved",
                    created_at=self._timestamp(),
                )
                session.add(row)
            return self._effect_from_model(row)

    def complete_tool_effect(
        self, *, task_id: str, lease_id: str, effect: ToolEffectRecord
    ) -> None:
        with self._sessions.begin() as session:
            self._require_live_lease(session, task_id, lease_id)
            row = session.scalar(
                select(RuntimeToolEffectModel).where(
                    RuntimeToolEffectModel.task_id == task_id,
                    RuntimeToolEffectModel.effect_id == effect.effect_id,
                )
            )
            if row is None:
                raise ValueError("effect must be reserved before completion")
            row.status = effect.status
            row.artifact_json = self._artifact_json(effect.artifact)
            row.evidence_json = self._evidence_json(effect.evidence)
            row.error_code = effect.error_code
            row.error_message = effect.error_message
            row.completed_at = self._timestamp()

    def _locked_task(self, session: Session, task_id: str) -> RuntimeAgentTaskModel:
        row = session.scalar(
            select(RuntimeAgentTaskModel)
            .where(RuntimeAgentTaskModel.id == task_id)
            .with_for_update()
        )
        if row is None:
            raise TaskNotFoundError(task_id)
        return row

    def _task_or_raise(self, session: Session, task_id: str) -> RuntimeAgentTaskModel:
        row = session.get(RuntimeAgentTaskModel, task_id)
        if row is None:
            raise TaskNotFoundError(task_id)
        return row

    def _require_live_lease(
        self, session: Session, task_id: str, lease_id: str
    ) -> RuntimeAgentTaskModel:
        row = self._locked_task(session, task_id)
        if (
            row.lease_id != lease_id
            or row.lease_expires_at is None
            or self._as_utc(row.lease_expires_at) <= self._timestamp()
        ):
            raise LeaseConflictError("lease must be live before persisting")
        return row

    def _append_event(
        self,
        session: Session,
        *,
        task_id: str,
        tick_id: str | None,
        kind: str,
        payload: dict[str, Any],
        event_id: str | None = None,
        tick_sequence: int | None = None,
        decision: Decision | None = None,
        tick_status: TickStatus | None = None,
        created_at: datetime | None = None,
    ) -> None:
        next_sequence = (
            int(
                session.scalar(
                    select(
                        func.coalesce(func.max(RuntimeTickEventModel.sequence), 0)
                    ).where(RuntimeTickEventModel.task_id == task_id)
                )
                or 0
            )
            + 1
        )
        session.add(
            RuntimeTickEventModel(
                id=event_id or self._id("event"),
                task_id=task_id,
                tick_id=tick_id,
                sequence=next_sequence,
                kind=kind,
                payload_json=self._json_value(payload),
                tick_sequence=tick_sequence,
                decision_json=(
                    self._decision_json(decision) if decision is not None else None
                ),
                tick_status=tick_status.value if tick_status is not None else None,
                created_at=created_at or self._timestamp(),
            )
        )

    def _latest_checkpoint(
        self, session: Session, task_id: str
    ) -> RuntimeCheckpointModel | None:
        return session.scalar(
            select(RuntimeCheckpointModel)
            .where(RuntimeCheckpointModel.task_id == task_id)
            .order_by(RuntimeCheckpointModel.checkpoint_version.desc())
            .limit(1)
        )

    @staticmethod
    def _id(prefix: str) -> str:
        return f"{prefix}_{uuid4().hex}"

    def _timestamp(self) -> datetime:
        return self._as_utc(self._now())

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return (
            value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
        )

    @staticmethod
    def _json_value(value: Any) -> Any:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))

    @classmethod
    def _task_model(cls, task: AgentTask) -> RuntimeAgentTaskModel:
        return RuntimeAgentTaskModel(
            id=task.task_id,
            goal=task.goal,
            repository_root=task.repository_root,
            profile=task.profile.value,
            budget_json=cls._budget_json(task.budget),
            status=task.status.value,
            final_report_json=cls._final_report_json(task.final_report),
            cancellation_requested=task.cancellation_requested,
            created_at=task.created_at,
            updated_at=task.updated_at,
        )

    @classmethod
    def _copy_task_to_model(cls, task: AgentTask, row: RuntimeAgentTaskModel) -> None:
        row.goal = task.goal
        row.repository_root = task.repository_root
        row.profile = task.profile.value
        row.budget_json = cls._budget_json(task.budget)
        row.status = task.status.value
        row.final_report_json = cls._final_report_json(task.final_report)
        row.cancellation_requested = task.cancellation_requested
        row.updated_at = task.updated_at

    @classmethod
    def _task_from_model(cls, row: RuntimeAgentTaskModel) -> AgentTask:
        return AgentTask(
            task_id=row.id,
            goal=row.goal,
            repository_root=row.repository_root,
            profile=TaskProfile(row.profile),
            budget=cls._budget_from_json(row.budget_json),
            status=TaskStatus(row.status),
            created_at=cls._as_utc(row.created_at),
            updated_at=cls._as_utc(row.updated_at),
            final_report=cls._final_report_from_json(row.final_report_json),
            cancellation_requested=row.cancellation_requested,
        )

    @classmethod
    def _event_from_model(cls, row: RuntimeTickEventModel) -> Event:
        return Event(
            event_id=row.id,
            task_id=row.task_id,
            tick_id=row.tick_id or "",
            sequence=row.sequence,
            kind=row.kind,
            payload=dict(row.payload_json or {}),
            created_at=cls._as_utc(row.created_at),
        )

    @classmethod
    def _tick_from_event(cls, row: RuntimeTickEventModel) -> Tick:
        if (
            row.tick_id is None
            or row.tick_sequence is None
            or row.decision_json is None
            or row.tick_status is None
        ):
            raise RuntimeError("completed tick event is missing its durable projection")
        return Tick(
            tick_id=row.tick_id,
            task_id=row.task_id,
            sequence=row.tick_sequence,
            decision=cls._decision_from_json(row.decision_json),
            status=TickStatus(row.tick_status),
            created_at=cls._as_utc(row.created_at),
        )

    @classmethod
    def _artifact_from_model(cls, row: RuntimeArtifactModel) -> Artifact:
        return Artifact(
            artifact_id=row.id,
            task_id=row.task_id,
            tick_id=row.tick_id,
            tool_name=row.tool_name,
            content=dict(row.content_json or {}),
            content_hash=row.content_hash,
            created_at=cls._as_utc(row.created_at),
        )

    @classmethod
    def _evidence_from_model(cls, row: RuntimeEvidenceModel) -> Evidence:
        return Evidence(
            evidence_id=row.id,
            task_id=row.task_id,
            artifact_id=row.artifact_id,
            source=row.source,
            summary=row.summary,
            created_at=cls._as_utc(row.created_at),
        )

    @classmethod
    def _usage_from_model(cls, row: RuntimeUsageModel) -> Usage:
        return Usage(
            usage_id=row.id,
            task_id=row.task_id,
            tick_id=row.tick_id,
            purpose=row.purpose,
            model_tier=row.model_tier,
            route_reason=row.route_reason,
            estimated_input_tokens=row.estimated_input_tokens,
            reserved_tokens=row.reserved_tokens,
            actual_input_tokens=row.actual_input_tokens,
            actual_output_tokens=row.actual_output_tokens,
            budget_mode=row.budget_mode,
            created_at=cls._as_utc(row.created_at),
        )

    @classmethod
    def _effect_from_model(cls, row: RuntimeToolEffectModel) -> ToolEffectRecord:
        return ToolEffectRecord(
            effect_id=row.effect_id,
            task_id=row.task_id,
            tool_name=row.tool_name,
            status=row.status,
            artifact=cls._artifact_from_json(row.artifact_json),
            evidence=cls._evidence_from_json(row.evidence_json),
            error_code=row.error_code,
            error_message=row.error_message,
        )

    @staticmethod
    def _artifact_json(value: Artifact | None) -> dict[str, object] | None:
        if value is None:
            return None
        return {
            "artifact_id": value.artifact_id,
            "task_id": value.task_id,
            "tick_id": value.tick_id,
            "tool_name": value.tool_name,
            "content": DurableRuntimeStore._json_value(value.content),
            "content_hash": value.content_hash,
            "created_at": value.created_at.isoformat(),
        }

    @staticmethod
    def _evidence_json(value: Evidence | None) -> dict[str, object] | None:
        if value is None:
            return None
        return {
            "evidence_id": value.evidence_id,
            "task_id": value.task_id,
            "artifact_id": value.artifact_id,
            "source": value.source,
            "summary": value.summary,
            "created_at": value.created_at.isoformat(),
        }

    @staticmethod
    def _artifact_from_json(value: dict[str, object] | None) -> Artifact | None:
        if value is None:
            return None
        return Artifact(
            artifact_id=str(value["artifact_id"]),
            task_id=str(value["task_id"]),
            tick_id=str(value["tick_id"]),
            tool_name=str(value["tool_name"]),
            content=dict(value.get("content") or {}),
            content_hash=str(value["content_hash"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
        )

    @staticmethod
    def _evidence_from_json(value: dict[str, object] | None) -> Evidence | None:
        if value is None:
            return None
        return Evidence(
            evidence_id=str(value["evidence_id"]),
            task_id=str(value["task_id"]),
            artifact_id=str(value["artifact_id"]),
            source=str(value["source"]),
            summary=str(value["summary"]),
            created_at=datetime.fromisoformat(str(value["created_at"])),
        )

    @staticmethod
    def _budget_json(budget: TaskBudget) -> dict[str, object]:
        return {
            "total_tokens": budget.total_tokens,
            "max_ticks": budget.max_ticks,
            "output_reserve_tokens": budget.output_reserve_tokens,
            "consumed_tokens": budget.consumed_tokens,
        }

    @staticmethod
    def _budget_from_json(value: dict[str, object] | None) -> TaskBudget:
        raw = value or {}
        return TaskBudget(
            total_tokens=int(raw["total_tokens"]),
            max_ticks=int(raw["max_ticks"]),
            output_reserve_tokens=int(raw.get("output_reserve_tokens", 512)),
            consumed_tokens=int(raw.get("consumed_tokens", 0)),
        )

    @staticmethod
    def _final_report_json(report: FinalReport | None) -> dict[str, object] | None:
        if report is None:
            return None
        return {
            "root_cause": report.root_cause,
            "repair_recommendation": report.repair_recommendation,
            "evidence_ids": list(report.evidence_ids),
        }

    @staticmethod
    def _final_report_from_json(value: dict[str, object] | None) -> FinalReport | None:
        if value is None:
            return None
        return FinalReport(
            root_cause=str(value["root_cause"]),
            repair_recommendation=str(value["repair_recommendation"]),
            evidence_ids=tuple(str(item) for item in value.get("evidence_ids", [])),
        )

    @staticmethod
    def _decision_json(decision: Decision) -> dict[str, object]:
        return {
            "kind": decision.kind.value,
            "reason_code": decision.reason_code,
            "tool_name": decision.tool_name,
            "arguments": dict(decision.arguments),
            "response": decision.response,
        }

    @staticmethod
    def _decision_from_json(value: dict[str, object]) -> Decision:
        return Decision(
            kind=DecisionKind(str(value["kind"])),
            reason_code=str(value["reason_code"]),
            tool_name=(
                str(value["tool_name"]) if value.get("tool_name") is not None else None
            ),
            arguments=dict(value.get("arguments") or {}),
            response=(
                str(value["response"]) if value.get("response") is not None else None
            ),
        )

    @staticmethod
    def _working_state_json(state: WorkingState) -> dict[str, object]:
        return {
            "plan": list(state.plan),
            "pending_items": list(state.pending_items),
            "evidence_ids": list(state.evidence_ids),
            "running_summary": state.running_summary,
            "human_input": state.human_input,
            "compaction_count": state.compaction_count,
        }

    @staticmethod
    def _working_state_from_json(value: dict[str, object] | None) -> WorkingState:
        raw = value or {}
        return WorkingState(
            plan=tuple(str(item) for item in raw.get("plan", [])),
            pending_items=tuple(str(item) for item in raw.get("pending_items", [])),
            evidence_ids=tuple(str(item) for item in raw.get("evidence_ids", [])),
            running_summary=str(raw.get("running_summary", "")),
            human_input=(
                str(raw["human_input"]) if raw.get("human_input") is not None else None
            ),
            compaction_count=int(raw.get("compaction_count", 0)),
        )

    @staticmethod
    def _context_json(context: ContextSnapshot) -> dict[str, object]:
        return {
            "task_id": context.task_id,
            "tick_sequence": context.tick_sequence,
            "payload": DurableRuntimeStore._json_value(context.payload),
            "estimated_input_tokens": context.estimated_input_tokens,
            "input_budget_tokens": context.input_budget_tokens,
            "output_reserve_tokens": context.output_reserve_tokens,
            "compacted": context.compacted,
            "omitted_event_count": context.omitted_event_count,
            "compaction_count": context.compaction_count,
            "tool_schemas": DurableRuntimeStore._json_value(context.tool_schemas),
        }

    @staticmethod
    def _context_from_json(value: dict[str, object] | None) -> ContextSnapshot:
        if value is None:
            raise ValueError("context checkpoint is missing")
        return ContextSnapshot(
            task_id=str(value["task_id"]),
            tick_sequence=int(value["tick_sequence"]),
            payload=dict(value.get("payload") or {}),
            estimated_input_tokens=int(value["estimated_input_tokens"]),
            input_budget_tokens=int(value["input_budget_tokens"]),
            output_reserve_tokens=int(value["output_reserve_tokens"]),
            compacted=bool(value["compacted"]),
            omitted_event_count=int(value["omitted_event_count"]),
            compaction_count=int(value.get("compaction_count", 0)),
            tool_schemas=tuple(
                dict(item)
                for item in value.get("tool_schemas", [])
                if isinstance(item, dict)
            ),
        )
