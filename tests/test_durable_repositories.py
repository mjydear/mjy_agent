"""Durable task repository correctness tests using an in-memory async database."""

from __future__ import annotations

import asyncio

import pytest

from athena.api.repositories import (
    Database,
    DurableIdempotencyConflictError,
    OutboxRepository,
    TaskCreate,
    TaskRepository,
)
from athena.api.repositories.task_repository import (
    AlertTaskCreate,
    TaskLeaseLostError,
)
from athena.config import DatabaseSettings


def _task(task_id: str = "ops-durable-1") -> TaskCreate:
    return TaskCreate(
        task_id=task_id,
        tenant_id="tenant-a",
        objective="diagnose payment CrashLoopBackOff",
        environment_id="env-prod",
        environment_mode="mock",
        scope={"namespace": "payment"},
        policy_snapshot={"readonly": True, "version": "policy-v1"},
        config_snapshot={"model": "rules-only", "tool_set": "k8s-v1"},
        budget={"remaining_steps": 4, "remaining_tokens": 6000},
        execution_profile="bounded_policy_loop",
        traceparent="00-0123456789abcdef0123456789abcdef-0123456789abcdef-01",
    )


@pytest.mark.asyncio
async def test_durable_task_command_is_idempotent_and_emits_outbox() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    outbox = OutboxRepository(database.session_factory)

    created, replayed = await tasks.create_task(
        _task(),
        idempotency_key="idem-create-1",
        request_hash="request-hash-a",
    )
    assert replayed is False
    assert created.status == "queued"
    assert created.policy_snapshot["version"] == "policy-v1"
    assert created.config_snapshot["tool_set"] == "k8s-v1"

    replay, replayed = await tasks.create_task(
        _task(),
        idempotency_key="idem-create-1",
        request_hash="request-hash-a",
    )
    assert replayed is True
    assert replay.task_id == created.task_id
    with pytest.raises(DurableIdempotencyConflictError):
        await tasks.create_task(
            _task(),
            idempotency_key="idem-create-1",
            request_hash="different-request",
        )

    events = await tasks.list_events_after("tenant-a", created.task_id)
    assert [(event["sequence"], event["event_type"]) for event in events] == [
        (1, "task.created")
    ]
    messages = await outbox.claim_batch("relay-a", limit=10)
    assert len(messages) == 1
    assert messages[0].event_type == "ops.task.created"
    assert messages[0].payload["task_id"] == created.task_id
    assert await outbox.mark_published(messages[0].message_id, "relay-a") is True
    assert await outbox.claim_batch("relay-b", limit=10) == ()
    await database.dispose()


@pytest.mark.asyncio
async def test_alert_receipt_deduplicates_active_fingerprint() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    command = AlertTaskCreate(
        task=_task("ops-alert-1"),
        integration_id="alertmanager-main",
        payload_hash="payload-a",
        canonical_fingerprint="fingerprint-a",
        payload={"alert_name": "KubePodCrashLooping"},
    )
    first = await tasks.create_alert_task(command)
    assert first.created is True
    assert first.duplicate is False

    exact_duplicate = await tasks.create_alert_task(command)
    assert exact_duplicate.duplicate is True
    assert exact_duplicate.task.task_id == first.task.task_id

    same_incident = await tasks.create_alert_task(
        AlertTaskCreate(
            task=_task("ops-alert-should-not-exist"),
            integration_id="alertmanager-main",
            payload_hash="payload-b",
            canonical_fingerprint="fingerprint-a",
            payload={"alert_name": "KubePodCrashLooping", "repeat": 2},
        )
    )
    assert same_incident.created is False
    assert same_incident.duplicate is True
    assert same_incident.task.task_id == first.task.task_id
    await database.dispose()


@pytest.mark.asyncio
async def test_checkpoint_rejects_stale_worker_with_fencing_generation() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    await tasks.create_task(_task())

    claim = await tasks.claim_next("worker-a", lease_seconds=30)
    assert claim is not None
    assert claim.lease_generation == 1
    completed = await tasks.checkpoint(
        "tenant-a",
        claim.task_id,
        worker_id="worker-a",
        expected_state_version=claim.state_version,
        lease_generation=claim.lease_generation,
        state={"root_cause": "database connection refused"},
        phase="report",
        status="succeeded",
        event_type="task.completed",
    )
    assert completed.status == "succeeded"
    assert completed.checkpoint_version == 1
    with pytest.raises(TaskLeaseLostError):
        await tasks.checkpoint(
            "tenant-a",
            claim.task_id,
            worker_id="worker-a",
            expected_state_version=claim.state_version,
            lease_generation=claim.lease_generation,
            state={},
            phase="report",
            status="succeeded",
        )
    events = await tasks.list_events_after("tenant-a", claim.task_id)
    assert [event["event_type"] for event in events] == [
        "task.created",
        "task.claimed",
        "task.completed",
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_expired_lease_can_be_reclaimed_and_fences_old_worker() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    await tasks.create_task(_task())

    first = await tasks.claim_next("worker-a", lease_seconds=0)
    assert first is not None
    assert first.lease_generation == 1
    reclaimed = await tasks.claim_next("worker-b", lease_seconds=30)
    assert reclaimed is not None
    assert reclaimed.task_id == first.task_id
    assert reclaimed.lease_owner == "worker-b"
    assert reclaimed.lease_generation == 2
    with pytest.raises(TaskLeaseLostError):
        await tasks.checkpoint(
            "tenant-a",
            first.task_id,
            worker_id="worker-a",
            expected_state_version=first.state_version,
            lease_generation=first.lease_generation,
            state={"summary": "stale write"},
            phase="report",
            status="succeeded",
        )

    completed = await tasks.checkpoint(
        "tenant-a",
        reclaimed.task_id,
        worker_id="worker-b",
        expected_state_version=reclaimed.state_version,
        lease_generation=reclaimed.lease_generation,
        state={"summary": "recovered by second worker"},
        phase="report",
        status="succeeded",
        event_type="task.completed",
    )
    assert completed.status == "succeeded"
    assert completed.checkpoint_version == 1
    events = await tasks.list_events_after("tenant-a", completed.task_id)
    assert [event["event_type"] for event in events] == [
        "task.created",
        "task.claimed",
        "task.claimed",
        "task.completed",
    ]
    await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_claim_only_grants_one_worker_lease() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    await tasks.create_task(_task())

    claims = await asyncio.gather(
        tasks.claim_next("worker-a", lease_seconds=30),
        tasks.claim_next("worker-b", lease_seconds=30),
    )
    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    assert winners[0].lease_owner in {"worker-a", "worker-b"}
    persisted = await tasks.get_task("tenant-a", winners[0].task_id)
    assert persisted is not None
    assert persisted.lease_generation == 1
    assert persisted.status == "running"
    await database.dispose()
