"""Transactional repositories for durable OpsTask commands and execution facts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import (
    AlertInstanceModel,
    AlertReceiptModel,
    IdempotencyRecordModel,
    OpsTaskModel,
    OutboxMessageModel,
    TaskCheckpointModel,
    TaskEventModel,
    TaskExecutionSnapshotModel,
)


class DurableIdempotencyConflictError(RuntimeError):
    """The same durable idempotency key was used with a different payload."""


class TaskLeaseLostError(RuntimeError):
    """A worker attempted to persist after its lease or fencing generation expired."""


@dataclass(frozen=True)
class TaskCreate:
    task_id: str
    tenant_id: str
    objective: str
    environment_id: str
    environment_mode: str
    scope: dict[str, object]
    policy_snapshot: dict[str, object]
    config_snapshot: dict[str, object]
    budget: dict[str, object]
    execution_profile: str
    workflow_type: str = "crashloop"
    trigger_type: str = "api"
    trigger_ref: str | None = None
    traceparent: str | None = None


@dataclass(frozen=True)
class PersistedTask:
    task_id: str
    tenant_id: str
    objective: str
    environment_id: str
    environment_mode: str
    scope: dict[str, object]
    policy_snapshot: dict[str, object]
    config_snapshot: dict[str, object]
    budget: dict[str, object]
    state: dict[str, object]
    execution_profile: str
    status: str
    phase: str
    state_version: int
    lease_owner: str | None
    lease_expires_at: datetime | None
    lease_generation: int
    checkpoint_version: int
    attempt_count: int
    traceparent: str | None
    # Kept at the end with a default for compatibility with existing task fakes.
    workflow_type: str = "crashloop"


@dataclass(frozen=True)
class AlertTaskCreate:
    task: TaskCreate
    integration_id: str
    payload_hash: str
    canonical_fingerprint: str
    payload: dict[str, object]
    external_event_id: str | None = None
    fingerprint_version: str = "v1"


@dataclass(frozen=True)
class AlertAcceptance:
    receipt_id: str
    task: PersistedTask
    created: bool
    duplicate: bool


@dataclass(frozen=True)
class OutboxMessage:
    message_id: str
    tenant_id: str
    aggregate_id: str
    event_type: str
    payload: dict[str, object]
    traceparent: str | None
    attempts: int


def _now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _task_from_model(task: OpsTaskModel) -> PersistedTask:
    return PersistedTask(
        task_id=task.id,
        tenant_id=task.tenant_id,
        objective=task.objective,
        environment_id=task.environment_id,
        environment_mode=task.environment_mode,
        scope=dict(task.scope_json or {}),
        policy_snapshot=dict(task.policy_snapshot_json or {}),
        config_snapshot=dict(task.config_snapshot_json or {}),
        budget=dict(task.budget_json or {}),
        state=dict(task.state_json or {}),
        execution_profile=task.execution_profile,
        status=task.status,
        phase=task.phase,
        state_version=task.state_version,
        lease_owner=task.lease_owner,
        lease_expires_at=task.lease_expires_at,
        lease_generation=task.lease_generation,
        checkpoint_version=task.checkpoint_version,
        attempt_count=task.attempt_count,
        traceparent=task.traceparent,
        workflow_type=task.workflow_type,
    )


class TaskRepository:
    """Task, snapshot, event and alert command repository with explicit transactions."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def create_task(
        self,
        command: TaskCreate,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
        operation: str = "ops-task:create",
    ) -> tuple[PersistedTask, bool]:
        """Create Task/Snapshot/Event/Outbox atomically or replay a durable command."""
        try:
            async with self._sessions() as session:
                async with session.begin():
                    replay = await self._lookup_idempotency(
                        session,
                        command.tenant_id,
                        idempotency_key,
                        operation,
                        request_hash,
                    )
                    if replay is not None:
                        task = await session.get(OpsTaskModel, replay["task_id"])
                        if task is None:
                            raise RuntimeError(
                                "idempotency record references missing task"
                            )
                        return _task_from_model(task), True
                    task = self._new_task(command)
                    session.add(task)
                    self._add_snapshot(session, task)
                    await self._append_event(
                        session,
                        task,
                        "task.created",
                        {"phase": task.phase, "trigger_type": task.trigger_type},
                    )
                    self._add_outbox(session, task, "ops.task.created")
                    if idempotency_key:
                        session.add(
                            IdempotencyRecordModel(
                                id=_id("idem"),
                                tenant_id=command.tenant_id,
                                operation=operation,
                                idempotency_key=idempotency_key,
                                request_hash=request_hash or "",
                                response_json={"task_id": command.task_id},
                            )
                        )
                return _task_from_model(task), False
        except IntegrityError:
            if not idempotency_key:
                raise
            return await self._replay_after_integrity_error(
                command.tenant_id, idempotency_key, operation, request_hash
            )

    async def create_alert_task(self, command: AlertTaskCreate) -> AlertAcceptance:
        """Persist alert receipt, deduplication state, Task and Outbox as one command."""
        try:
            async with self._sessions() as session:
                async with session.begin():
                    existing_receipt = await session.scalar(
                        select(AlertReceiptModel).where(
                            AlertReceiptModel.tenant_id == command.task.tenant_id,
                            AlertReceiptModel.integration_id == command.integration_id,
                            AlertReceiptModel.payload_hash == command.payload_hash,
                        )
                    )
                    if existing_receipt is not None:
                        task = await session.get(OpsTaskModel, existing_receipt.task_id)
                        if task is None:
                            raise RuntimeError("alert receipt references missing task")
                        return AlertAcceptance(
                            receipt_id=existing_receipt.id,
                            task=_task_from_model(task),
                            created=False,
                            duplicate=True,
                        )

                    instance = await session.scalar(
                        select(AlertInstanceModel)
                        .where(
                            AlertInstanceModel.tenant_id == command.task.tenant_id,
                            AlertInstanceModel.canonical_fingerprint
                            == command.canonical_fingerprint,
                            AlertInstanceModel.fingerprint_version
                            == command.fingerprint_version,
                            AlertInstanceModel.status == "active",
                        )
                        .with_for_update()
                    )
                    if instance is None:
                        task = self._new_task(command.task)
                        session.add(task)
                        self._add_snapshot(session, task)
                        await self._append_event(
                            session,
                            task,
                            "task.created",
                            {"phase": task.phase, "trigger_type": "alert"},
                        )
                        self._add_outbox(session, task, "ops.task.created")
                        instance = AlertInstanceModel(
                            id=_id("alert-instance"),
                            tenant_id=command.task.tenant_id,
                            canonical_fingerprint=command.canonical_fingerprint,
                            fingerprint_version=command.fingerprint_version,
                            status="active",
                            task_id=task.id,
                        )
                        session.add(instance)
                        created = True
                    else:
                        task = await session.get(OpsTaskModel, instance.task_id)
                        if task is None:
                            raise RuntimeError("alert instance references missing task")
                        instance.last_seen_at = _now()
                        created = False
                    receipt = AlertReceiptModel(
                        id=_id("alert-receipt"),
                        tenant_id=command.task.tenant_id,
                        integration_id=command.integration_id,
                        payload_hash=command.payload_hash,
                        external_event_id=command.external_event_id,
                        canonical_fingerprint=command.canonical_fingerprint,
                        task_id=task.id,
                        payload_json=dict(command.payload),
                    )
                    session.add(receipt)
                return AlertAcceptance(
                    receipt_id=receipt.id,
                    task=_task_from_model(task),
                    created=created,
                    duplicate=not created,
                )
        except IntegrityError:
            return await self._alert_replay_after_integrity_error(command)

    async def get_task(self, tenant_id: str, task_id: str) -> PersistedTask | None:
        async with self._sessions() as session:
            task = await session.scalar(
                select(OpsTaskModel).where(
                    OpsTaskModel.id == task_id, OpsTaskModel.tenant_id == tenant_id
                )
            )
            return _task_from_model(task) if task is not None else None

    async def list_tasks(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        environment_id: str | None = None,
        limit: int = 100,
    ) -> tuple[PersistedTask, ...]:
        async with self._sessions() as session:
            statement = select(OpsTaskModel).where(OpsTaskModel.tenant_id == tenant_id)
            if status is not None:
                statement = statement.where(OpsTaskModel.status == status)
            if environment_id is not None:
                statement = statement.where(
                    OpsTaskModel.environment_id == environment_id
                )
            rows = (
                await session.scalars(
                    statement.order_by(OpsTaskModel.created_at.desc()).limit(limit)
                )
            ).all()
            return tuple(_task_from_model(row) for row in rows)

    async def cancel_task(
        self,
        tenant_id: str,
        task_id: str,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[PersistedTask | None, bool]:
        async with self._sessions() as session:
            async with session.begin():
                operation = f"ops-task:{task_id}:cancel"
                replay = await self._lookup_idempotency(
                    session,
                    tenant_id,
                    idempotency_key,
                    operation,
                    request_hash,
                )
                if replay is not None:
                    task = await session.get(OpsTaskModel, replay["task_id"])
                    return (_task_from_model(task) if task is not None else None), True
                task = await session.scalar(
                    select(OpsTaskModel)
                    .where(
                        OpsTaskModel.id == task_id,
                        OpsTaskModel.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                if task is None:
                    return None, False
                if task.status in {"succeeded", "failed", "cancelled"}:
                    result = _task_from_model(task)
                else:
                    task.status = "cancelled"
                    task.phase = "report"
                    task.state_version += 1
                    task.lease_owner = None
                    task.lease_expires_at = None
                    await self._append_event(
                        session, task, "task.cancelled", {"phase": "report"}
                    )
                    self._add_outbox(session, task, "ops.task.cancelled")
                    result = _task_from_model(task)
                self._remember_idempotency(
                    session,
                    tenant_id,
                    idempotency_key,
                    operation,
                    request_hash,
                    result.task_id,
                )
                return result, False

    async def resume_with_input(
        self,
        tenant_id: str,
        task_id: str,
        content_hash: str,
        summary: str,
        *,
        idempotency_key: str | None = None,
        request_hash: str | None = None,
    ) -> tuple[PersistedTask | None, bool]:
        async with self._sessions() as session:
            async with session.begin():
                operation = f"ops-task:{task_id}:input"
                replay = await self._lookup_idempotency(
                    session,
                    tenant_id,
                    idempotency_key,
                    operation,
                    request_hash,
                )
                if replay is not None:
                    task = await session.get(OpsTaskModel, replay["task_id"])
                    return (_task_from_model(task) if task is not None else None), True
                task = await session.scalar(
                    select(OpsTaskModel)
                    .where(
                        OpsTaskModel.id == task_id,
                        OpsTaskModel.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                if task is None:
                    return None, False
                if task.status != "waiting":
                    raise ValueError("TASK_NOT_WAITING_INPUT")
                task.status = "queued"
                task.state_version += 1
                task.next_run_at = _now()
                state = dict(task.state_json or {})
                state["operator_input_hash"] = content_hash
                task.state_json = state
                await self._append_event(
                    session,
                    task,
                    "task.input_received",
                    {"summary": summary[:256], "content_hash": content_hash},
                )
                self._add_outbox(session, task, "ops.task.resumed")
                result = _task_from_model(task)
                self._remember_idempotency(
                    session,
                    tenant_id,
                    idempotency_key,
                    operation,
                    request_hash,
                    result.task_id,
                )
                return result, False

    async def list_events_after(
        self, tenant_id: str, task_id: str, after_sequence: int = 0
    ) -> tuple[dict[str, object], ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(TaskEventModel)
                    .where(
                        TaskEventModel.tenant_id == tenant_id,
                        TaskEventModel.task_id == task_id,
                        TaskEventModel.sequence > after_sequence,
                    )
                    .order_by(TaskEventModel.sequence)
                )
            ).all()
            return tuple(
                {
                    "sequence": event.sequence,
                    "event_type": event.event_type,
                    "data": dict(event.data_json or {}),
                    "trace_id": event.trace_id,
                    "created_at": event.created_at,
                }
                for event in rows
            )

    async def list_alert_receipts(
        self, tenant_id: str, *, limit: int = 100
    ) -> tuple[dict[str, object], ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(AlertReceiptModel)
                    .where(AlertReceiptModel.tenant_id == tenant_id)
                    .order_by(AlertReceiptModel.received_at.desc())
                    .limit(limit)
                )
            ).all()
        return tuple(
            {
                "receipt_id": receipt.id,
                "integration_id": receipt.integration_id,
                "payload_hash": receipt.payload_hash,
                "canonical_fingerprint": receipt.canonical_fingerprint,
                "task_id": receipt.task_id,
                "payload": dict(receipt.payload_json or {}),
                "received_at": receipt.received_at,
            }
            for receipt in rows
        )

    async def claim_next(
        self, worker_id: str, lease_seconds: int
    ) -> PersistedTask | None:
        now = _now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        async with self._sessions() as session:
            async with session.begin():
                candidate = await session.scalar(
                    select(OpsTaskModel)
                    .where(
                        OpsTaskModel.next_run_at <= now,
                        or_(
                            OpsTaskModel.status == "queued",
                            and_(
                                OpsTaskModel.status == "running",
                                OpsTaskModel.lease_expires_at.is_not(None),
                                OpsTaskModel.lease_expires_at < now,
                            ),
                        ),
                    )
                    .order_by(OpsTaskModel.next_run_at, OpsTaskModel.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if candidate is None:
                    return None
                result = await session.execute(
                    update(OpsTaskModel)
                    .where(
                        OpsTaskModel.id == candidate.id,
                        OpsTaskModel.tenant_id == candidate.tenant_id,
                        OpsTaskModel.state_version == candidate.state_version,
                        OpsTaskModel.next_run_at <= now,
                        or_(
                            OpsTaskModel.status == "queued",
                            and_(
                                OpsTaskModel.status == "running",
                                OpsTaskModel.lease_expires_at.is_not(None),
                                OpsTaskModel.lease_expires_at < now,
                            ),
                        ),
                    )
                    .values(
                        status="running",
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        lease_generation=candidate.lease_generation + 1,
                        attempt_count=candidate.attempt_count + 1,
                        state_version=candidate.state_version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    return None
                claimed = await session.scalar(
                    select(OpsTaskModel)
                    .where(OpsTaskModel.id == candidate.id)
                    .execution_options(populate_existing=True)
                )
                assert claimed is not None
                await self._append_event(
                    session,
                    claimed,
                    "task.claimed",
                    {
                        "worker_id": worker_id,
                        "lease_generation": claimed.lease_generation,
                    },
                )
                return _task_from_model(claimed)

    async def claim_task(
        self,
        tenant_id: str,
        task_id: str,
        worker_id: str,
        lease_seconds: int,
    ) -> PersistedTask | None:
        """Claim one delivered task reference or reject a duplicate/stale message."""
        now = _now()
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        async with self._sessions() as session:
            async with session.begin():
                task = await session.scalar(
                    select(OpsTaskModel)
                    .where(
                        OpsTaskModel.id == task_id,
                        OpsTaskModel.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                if task is None or _as_utc(task.next_run_at) > now:
                    return None
                eligible = task.status == "queued" or (
                    task.status == "running"
                    and task.lease_expires_at is not None
                    and _as_utc(task.lease_expires_at) < now
                )
                if not eligible:
                    return None
                result = await session.execute(
                    update(OpsTaskModel)
                    .where(
                        OpsTaskModel.id == task_id,
                        OpsTaskModel.tenant_id == tenant_id,
                        OpsTaskModel.state_version == task.state_version,
                        OpsTaskModel.next_run_at <= now,
                        or_(
                            OpsTaskModel.status == "queued",
                            and_(
                                OpsTaskModel.status == "running",
                                OpsTaskModel.lease_expires_at.is_not(None),
                                OpsTaskModel.lease_expires_at < now,
                            ),
                        ),
                    )
                    .values(
                        status="running",
                        lease_owner=worker_id,
                        lease_expires_at=lease_expires_at,
                        lease_generation=task.lease_generation + 1,
                        attempt_count=task.attempt_count + 1,
                        state_version=task.state_version + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                if result.rowcount != 1:
                    return None
                claimed = await session.scalar(
                    select(OpsTaskModel)
                    .where(
                        OpsTaskModel.id == task_id,
                        OpsTaskModel.tenant_id == tenant_id,
                    )
                    .execution_options(populate_existing=True)
                )
                assert claimed is not None
                await self._append_event(
                    session,
                    claimed,
                    "task.claimed",
                    {
                        "worker_id": worker_id,
                        "lease_generation": claimed.lease_generation,
                    },
                )
                return _task_from_model(claimed)

    async def checkpoint(
        self,
        tenant_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_state_version: int,
        lease_generation: int,
        state: dict[str, object],
        phase: str,
        status: str = "running",
        event_type: str = "task.checkpointed",
        event_data: dict[str, object] | None = None,
        next_run_at: datetime | None = None,
    ) -> PersistedTask:
        async with self._sessions() as session:
            async with session.begin():
                task = await session.scalar(
                    select(OpsTaskModel)
                    .where(
                        OpsTaskModel.id == task_id,
                        OpsTaskModel.tenant_id == tenant_id,
                    )
                    .with_for_update()
                )
                if (
                    task is None
                    or task.lease_owner != worker_id
                    or task.lease_generation != lease_generation
                    or task.state_version != expected_state_version
                    or task.lease_expires_at is None
                    or _as_utc(task.lease_expires_at) <= _now()
                ):
                    raise TaskLeaseLostError("TASK_STALE_WORKER")
                task.state_json = dict(state)
                task.phase = phase
                task.status = status
                task.state_version += 1
                task.checkpoint_version += 1
                task.next_run_at = next_run_at or _now()
                if status in {
                    "queued",
                    "succeeded",
                    "failed",
                    "cancelled",
                    "waiting",
                }:
                    task.lease_owner = None
                    task.lease_expires_at = None
                checkpoint = TaskCheckpointModel(
                    id=_id("checkpoint"),
                    tenant_id=tenant_id,
                    task_id=task_id,
                    checkpoint_version=task.checkpoint_version,
                    state_version=task.state_version,
                    lease_generation=lease_generation,
                    state_json=dict(state),
                )
                session.add(checkpoint)
                await self._append_event(
                    session, task, event_type, event_data or {"phase": phase}
                )
                self._add_outbox(session, task, "ops.task.checkpointed")
                return _task_from_model(task)

    async def requeue(
        self,
        tenant_id: str,
        task_id: str,
        *,
        worker_id: str,
        expected_state_version: int,
        lease_generation: int,
        delay_seconds: float,
        error_code: str,
    ) -> PersistedTask:
        next_run = _now() + timedelta(seconds=max(delay_seconds, 0.0))
        return await self.checkpoint(
            tenant_id,
            task_id,
            worker_id=worker_id,
            expected_state_version=expected_state_version,
            lease_generation=lease_generation,
            state={"last_error_code": error_code},
            phase="collect",
            status="queued",
            event_type="task.requeued",
            event_data={"error_code": error_code, "next_run_at": next_run.isoformat()},
            next_run_at=next_run,
        )

    def _new_task(self, command: TaskCreate) -> OpsTaskModel:
        return OpsTaskModel(
            id=command.task_id,
            tenant_id=command.tenant_id,
            workflow_type=command.workflow_type,
            objective=command.objective,
            environment_id=command.environment_id,
            environment_mode=command.environment_mode,
            scope_json=dict(command.scope),
            policy_snapshot_json=dict(command.policy_snapshot),
            config_snapshot_json=dict(command.config_snapshot),
            budget_json=dict(command.budget),
            state_json={},
            execution_profile=command.execution_profile,
            status="queued",
            phase="validate",
            trigger_type=command.trigger_type,
            trigger_ref=command.trigger_ref,
            traceparent=command.traceparent,
        )

    def _add_snapshot(self, session: AsyncSession, task: OpsTaskModel) -> None:
        session.add(
            TaskExecutionSnapshotModel(
                id=_id("snapshot"),
                tenant_id=task.tenant_id,
                task_id=task.id,
                snapshot_json={
                    "policy": dict(task.policy_snapshot_json or {}),
                    "config": dict(task.config_snapshot_json or {}),
                    "scope": dict(task.scope_json or {}),
                    "budget": dict(task.budget_json or {}),
                    "execution_profile": task.execution_profile,
                    "workflow_type": task.workflow_type,
                    "environment_mode": task.environment_mode,
                },
            )
        )

    def _add_outbox(
        self, session: AsyncSession, task: OpsTaskModel, event_type: str
    ) -> None:
        session.add(
            OutboxMessageModel(
                id=_id("outbox"),
                tenant_id=task.tenant_id,
                aggregate_id=task.id,
                event_type=event_type,
                payload_json={"task_id": task.id, "state_version": task.state_version},
                traceparent=task.traceparent,
            )
        )

    async def _append_event(
        self,
        session: AsyncSession,
        task: OpsTaskModel,
        event_type: str,
        data: dict[str, object],
    ) -> None:
        sequence = (
            int(
                await session.scalar(
                    select(func.coalesce(func.max(TaskEventModel.sequence), 0)).where(
                        TaskEventModel.tenant_id == task.tenant_id,
                        TaskEventModel.task_id == task.id,
                    )
                )
                or 0
            )
            + 1
        )
        session.add(
            TaskEventModel(
                id=_id("event"),
                tenant_id=task.tenant_id,
                task_id=task.id,
                sequence=sequence,
                event_type=event_type,
                data_json=dict(data),
                trace_id=task.traceparent,
            )
        )

    async def _lookup_idempotency(
        self,
        session: AsyncSession,
        tenant_id: str,
        idempotency_key: str | None,
        operation: str,
        request_hash: str | None,
    ) -> dict[str, object] | None:
        if not idempotency_key:
            return None
        record = await session.scalar(
            select(IdempotencyRecordModel).where(
                IdempotencyRecordModel.tenant_id == tenant_id,
                IdempotencyRecordModel.operation == operation,
                IdempotencyRecordModel.idempotency_key == idempotency_key,
            )
        )
        if record is None:
            return None
        if request_hash is not None and record.request_hash != request_hash:
            raise DurableIdempotencyConflictError("IDEMPOTENCY_KEY_REUSED")
        return dict(record.response_json or {})

    @staticmethod
    def _remember_idempotency(
        session: AsyncSession,
        tenant_id: str,
        idempotency_key: str | None,
        operation: str,
        request_hash: str | None,
        task_id: str,
    ) -> None:
        if not idempotency_key:
            return
        session.add(
            IdempotencyRecordModel(
                id=_id("idem"),
                tenant_id=tenant_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash or "",
                response_json={"task_id": task_id},
            )
        )

    async def _replay_after_integrity_error(
        self,
        tenant_id: str,
        idempotency_key: str,
        operation: str,
        request_hash: str | None,
    ) -> tuple[PersistedTask, bool]:
        async with self._sessions() as session:
            record = await session.scalar(
                select(IdempotencyRecordModel).where(
                    IdempotencyRecordModel.tenant_id == tenant_id,
                    IdempotencyRecordModel.operation == operation,
                    IdempotencyRecordModel.idempotency_key == idempotency_key,
                )
            )
            if record is None:
                raise RuntimeError(
                    "idempotency record was not committed after conflict"
                )
            if request_hash is not None and record.request_hash != request_hash:
                raise DurableIdempotencyConflictError("IDEMPOTENCY_KEY_REUSED")
            task = await session.get(OpsTaskModel, record.response_json["task_id"])
            if task is None:
                raise RuntimeError("idempotency record references missing task")
            return _task_from_model(task), True

    async def _alert_replay_after_integrity_error(
        self, command: AlertTaskCreate
    ) -> AlertAcceptance:
        async with self._sessions() as session:
            receipt = await session.scalar(
                select(AlertReceiptModel).where(
                    AlertReceiptModel.tenant_id == command.task.tenant_id,
                    AlertReceiptModel.integration_id == command.integration_id,
                    AlertReceiptModel.payload_hash == command.payload_hash,
                )
            )
            if receipt is None:
                raise RuntimeError("alert receipt was not committed after conflict")
            task = await session.get(OpsTaskModel, receipt.task_id)
            if task is None:
                raise RuntimeError("alert receipt references missing task")
            return AlertAcceptance(
                receipt_id=receipt.id,
                task=_task_from_model(task),
                created=False,
                duplicate=True,
            )


class OutboxRepository:
    """Claim and acknowledge the transactional outbox without assuming exactly-once publish."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def claim_batch(
        self, owner: str, *, limit: int, lock_seconds: int = 30
    ) -> tuple[OutboxMessage, ...]:
        now = _now()
        async with self._sessions() as session:
            async with session.begin():
                rows = (
                    await session.scalars(
                        select(OutboxMessageModel)
                        .where(
                            OutboxMessageModel.published_at.is_(None),
                            OutboxMessageModel.available_at <= now,
                            or_(
                                OutboxMessageModel.locked_until.is_(None),
                                OutboxMessageModel.locked_until < now,
                            ),
                        )
                        .order_by(OutboxMessageModel.created_at)
                        .limit(limit)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
                until = now + timedelta(seconds=lock_seconds)
                for row in rows:
                    row.lock_owner = owner
                    row.locked_until = until
                    row.attempts += 1
                return tuple(self._to_message(row) for row in rows)

    async def mark_published(self, message_id: str, owner: str) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(
                    OutboxMessageModel, message_id, with_for_update=True
                )
                if (
                    row is None
                    or row.lock_owner != owner
                    or row.published_at is not None
                ):
                    return False
                row.published_at = _now()
                row.lock_owner = None
                row.locked_until = None
                return True

    async def retry(
        self, message_id: str, owner: str, error: str, delay_seconds: float
    ) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                row = await session.get(
                    OutboxMessageModel, message_id, with_for_update=True
                )
                if (
                    row is None
                    or row.lock_owner != owner
                    or row.published_at is not None
                ):
                    return False
                row.last_error = error[:2000]
                row.available_at = _now() + timedelta(seconds=max(delay_seconds, 0.0))
                row.lock_owner = None
                row.locked_until = None
                return True

    @staticmethod
    def _to_message(row: OutboxMessageModel) -> OutboxMessage:
        return OutboxMessage(
            message_id=row.id,
            tenant_id=row.tenant_id,
            aggregate_id=row.aggregate_id,
            event_type=row.event_type,
            payload=dict(row.payload_json or {}),
            traceparent=row.traceparent,
            attempts=row.attempts,
        )
