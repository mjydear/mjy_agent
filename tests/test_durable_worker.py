"""Outbox-to-worker delivery tests with ACK-after-checkpoint semantics."""

from __future__ import annotations

import pytest

from athena.api.repositories import (
    Database,
    OutboxRepository,
    TaskCreate,
    TaskRepository,
)
from athena.application.durable_worker import DurableTaskWorker, WorkerOutcome
from athena.application.outbox_relay import OutboxRelay
from athena.config import DatabaseSettings
from athena.infra.task_stream import InMemoryTaskStream


def _command() -> TaskCreate:
    return TaskCreate(
        task_id="ops-worker-1",
        tenant_id="tenant-a",
        objective="diagnose CrashLoopBackOff",
        environment_id="env-a",
        environment_mode="mock",
        scope={"namespace": "default"},
        policy_snapshot={"readonly": True},
        config_snapshot={"tool_set": "k8s-v1"},
        budget={"remaining_steps": 4},
        execution_profile="bounded_policy_loop",
    )


@pytest.mark.asyncio
async def test_worker_acks_only_after_durable_checkpoint() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    outbox = OutboxRepository(database.session_factory)
    stream = InMemoryTaskStream()
    await tasks.create_task(_command())
    relay = OutboxRelay(outbox, stream, owner="relay-a")
    assert await relay.dispatch_once() == 1

    async def handler(task):
        assert task.lease_owner == "worker-a"
        return WorkerOutcome(state={"summary": "diagnosis completed"})

    worker = DurableTaskWorker(
        tasks,
        stream,
        handler,
        worker_id="worker-a",
        lease_ttl_seconds=30,
        max_attempts=3,
    )
    assert await worker.run_once(count=1, block_ms=0, reclaim_idle_ms=1) == 1
    persisted = await tasks.get_task("tenant-a", "ops-worker-1")
    assert persisted is not None
    assert persisted.status == "succeeded"
    assert persisted.checkpoint_version == 1
    assert persisted.lease_owner is None
    assert await stream.consume("observer", count=1, block_ms=0) == ()
    await database.dispose()


@pytest.mark.asyncio
async def test_worker_retry_republishes_from_checkpoint_outbox() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    outbox = OutboxRepository(database.session_factory)
    stream = InMemoryTaskStream()
    await tasks.create_task(_command())
    relay = OutboxRelay(outbox, stream, owner="relay-a")
    await relay.dispatch_once()

    async def retry_handler(task):
        return WorkerOutcome(
            state={}, retry_delay_seconds=0, error_code="UPSTREAM_UNAVAILABLE"
        )

    worker = DurableTaskWorker(
        tasks,
        stream,
        retry_handler,
        worker_id="worker-a",
        lease_ttl_seconds=30,
        max_attempts=3,
    )
    assert await worker.run_once(count=1, block_ms=0, reclaim_idle_ms=1) == 1
    requeued = await tasks.get_task("tenant-a", "ops-worker-1")
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.lease_owner is None
    assert await relay.dispatch_once() == 1
    next_message = await stream.consume("worker-b", count=1, block_ms=0)
    assert len(next_message) == 1
    assert next_message[0].task_id == "ops-worker-1"
    await database.dispose()


@pytest.mark.asyncio
async def test_worker_marks_task_failed_before_dead_lettering_exhausted_retry() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    outbox = OutboxRepository(database.session_factory)
    stream = InMemoryTaskStream()
    await tasks.create_task(_command())
    await OutboxRelay(outbox, stream, owner="relay-a").dispatch_once()

    async def retry_handler(task):
        return WorkerOutcome(
            state={}, retry_delay_seconds=0, error_code="UPSTREAM_DOWN"
        )

    worker = DurableTaskWorker(
        tasks,
        stream,
        retry_handler,
        worker_id="worker-a",
        lease_ttl_seconds=30,
        max_attempts=1,
    )
    assert await worker.run_once(count=1, block_ms=0, reclaim_idle_ms=1) == 1
    task = await tasks.get_task("tenant-a", "ops-worker-1")
    assert task is not None
    assert task.status == "failed"
    assert task.state["error_code"] == "UPSTREAM_DOWN"
    assert len(stream.dead_letters) == 1
    await database.dispose()
