"""Focused contract tests for the SQLAlchemy RuntimeStore adapter."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from athena.api.repositories.models import Base
from athena.runtime import (
    AgentRuntime,
    AgentTask,
    Artifact,
    ContextSnapshot,
    Decision,
    DecisionKind,
    Event,
    Evidence,
    TaskStatus,
    Tick,
    TickStatus,
    Usage,
    WorkingState,
)
from athena.runtime.durable import DurableRuntimeStore
from athena.runtime.store import LeaseConflictError


@pytest.fixture
def runtime_sessions():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    try:
        yield sessions
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_durable_store_recovers_committed_tick_after_store_restart(runtime_sessions) -> None:
    store = DurableRuntimeStore(runtime_sessions)
    task = AgentTask.create(goal="Find the failing calculation", repository_root="/repo")
    store.create_task(task)
    store.claim(task.task_id, "worker-a")

    task = replace(task, status=TaskStatus.RUNNING)
    tick, events, artifact, evidence, usage, state, context = _tick_aggregate(task)
    store.commit_tick(
        task_id=task.task_id,
        lease_id="worker-a",
        task=task,
        tick=tick,
        events=events,
        artifacts=(artifact,),
        evidence=(evidence,),
        usage=usage,
        working_state=state,
        context=context,
    )

    restarted_store = DurableRuntimeStore(runtime_sessions)
    snapshot = restarted_store.snapshot(task.task_id)

    assert [item.task_id for item in restarted_store.list_tasks()] == [task.task_id]
    assert snapshot.task.status is TaskStatus.RUNNING
    assert snapshot.ticks == (tick,)
    assert [item.sequence for item in snapshot.events] == [1, 2, 3, 4]
    assert snapshot.artifacts == (artifact,)
    assert snapshot.evidence == (evidence,)
    assert snapshot.usage == (usage,)
    assert snapshot.working_state == state
    assert snapshot.context == context


def test_existing_agent_runtime_advances_directly_against_durable_store(runtime_sessions) -> None:
    store = DurableRuntimeStore(runtime_sessions)
    task = AgentTask.create(goal="Diagnose the failing test", repository_root="/offline-repo")
    store.create_task(task)

    result = AgentRuntime(store=store).advance(task.task_id, lease_id="worker-a")
    snapshot = store.snapshot(task.task_id)

    assert result.tick is not None
    assert result.tick.sequence == 1
    assert len(snapshot.ticks) == 1
    assert len(snapshot.artifacts) == 1
    assert len(snapshot.evidence) == 1
    assert len(snapshot.usage) == 1


def test_expired_lease_can_be_reclaimed_and_fences_the_previous_worker(runtime_sessions) -> None:
    clock = [datetime(2026, 8, 9, tzinfo=UTC)]
    store = DurableRuntimeStore(runtime_sessions, lease_seconds=10, now=lambda: clock[0])
    task = AgentTask.create(goal="Recover after a worker crash", repository_root="/repo")
    store.create_task(task)
    store.claim(task.task_id, "worker-a")

    clock[0] += timedelta(seconds=11)
    assert store.claim(task.task_id, "worker-b").task_id == task.task_id

    with pytest.raises(LeaseConflictError, match="lease must be live"):
        store.persist_task(
            task_id=task.task_id,
            lease_id="worker-a",
            task=replace(task, status=TaskStatus.CANCELLED),
            kind="task.cancelled",
            payload={},
        )

    store.persist_task(
        task_id=task.task_id,
        lease_id="worker-b",
        task=replace(task, status=TaskStatus.CANCELLED),
        kind="task.cancelled",
        payload={},
    )
    assert store.snapshot(task.task_id).task.status is TaskStatus.CANCELLED


def test_failed_aggregate_commit_rolls_back_task_events_and_checkpoint(runtime_sessions) -> None:
    store = DurableRuntimeStore(runtime_sessions)
    task = AgentTask.create(goal="Keep each Tick atomic", repository_root="/repo")
    store.create_task(task)
    store.claim(task.task_id, "worker-a")
    task = replace(task, status=TaskStatus.RUNNING)
    tick, events, artifact, evidence, usage, state, context = _tick_aggregate(task)
    store.commit_tick(
        task_id=task.task_id,
        lease_id="worker-a",
        task=task,
        tick=tick,
        events=events,
        artifacts=(artifact,),
        evidence=(evidence,),
        usage=usage,
        working_state=state,
        context=context,
    )

    second = _tick_aggregate(task, sequence=2, artifact_id=artifact.artifact_id)
    with pytest.raises(IntegrityError):
        store.commit_tick(
            task_id=task.task_id,
            lease_id="worker-a",
            task=task,
            tick=second[0],
            events=second[1],
            artifacts=(second[2],),
            evidence=(second[3],),
            usage=second[4],
            working_state=second[5],
            context=second[6],
        )

    snapshot = store.snapshot(task.task_id)
    assert len(snapshot.ticks) == 1
    assert len(snapshot.events) == 4
    assert len(snapshot.artifacts) == 1
    assert len(snapshot.usage) == 1


def _tick_aggregate(task: AgentTask, *, sequence: int = 1, artifact_id: str = "artifact-1"):
    created_at = datetime(2026, 8, 9, 12, sequence, tzinfo=UTC)
    tick_id = f"tick-{sequence}"
    decision = Decision(
        kind=DecisionKind.TOOL_CALL,
        reason_code="TEST_DECISION",
        tool_name="search_code",
        arguments={"query": "discount"},
    )
    tick = Tick(
        tick_id=tick_id,
        task_id=task.task_id,
        sequence=sequence,
        decision=decision,
        status=TickStatus.COMPLETED,
        created_at=created_at,
    )
    events = (
        Event("event-start-" + str(sequence), task.task_id, tick_id, 0, "tick.started", {}, created_at),
        Event("event-tool-" + str(sequence), task.task_id, tick_id, 0, "tool.succeeded", {}, created_at),
        Event(
            "event-complete-" + str(sequence),
            task.task_id,
            tick_id,
            0,
            "tick.completed",
            {"decision": decision.to_public_payload(), "status": "completed"},
            created_at,
        ),
    )
    artifact = Artifact(
        artifact_id=artifact_id,
        task_id=task.task_id,
        tick_id=tick_id,
        tool_name="search_code",
        content={"matches": ["pricing.py:10"]},
        content_hash="hash-" + str(sequence),
        created_at=created_at,
    )
    evidence = Evidence(
        evidence_id="evidence-" + str(sequence),
        task_id=task.task_id,
        artifact_id=artifact_id,
        source="tool:search_code",
        summary="Found the target code.",
        created_at=created_at,
    )
    usage = Usage(
        usage_id="usage-" + str(sequence),
        task_id=task.task_id,
        tick_id=tick_id,
        purpose="react_decision",
        model_tier="economy",
        route_reason="TEST_ROUTE",
        estimated_input_tokens=100,
        reserved_tokens=612,
        actual_input_tokens=100,
        actual_output_tokens=30,
        budget_mode="NORMAL",
        created_at=created_at,
    )
    state = WorkingState(
        plan=("inspect",),
        pending_items=("report",),
        evidence_ids=(evidence.evidence_id,),
        running_summary="Search result retained as Evidence.",
    )
    context = ContextSnapshot(
        task_id=task.task_id,
        tick_sequence=sequence,
        payload={"task": {"goal": task.goal}},
        estimated_input_tokens=100,
        input_budget_tokens=1000,
        output_reserve_tokens=512,
        compacted=False,
        omitted_event_count=0,
    )
    return tick, events, artifact, evidence, usage, state, context
