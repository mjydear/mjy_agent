"""Public behavior tests for the offline Agent Runtime core."""

from __future__ import annotations

from athena.runtime import (
    AgentRuntime,
    AgentTask,
    DecisionKind,
    InMemoryRuntimeStore,
    TaskStatus,
)


def test_advance_records_one_read_only_tool_tick_with_evidence_and_usage() -> None:
    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal="Diagnose why the payment refund test fails",
        repository_root="/controlled/repository",
    )
    store.create_task(task)
    runtime = AgentRuntime(store=store)

    result = runtime.advance(task.task_id, lease_id="worker-a")
    snapshot = store.snapshot(task.task_id)

    assert result.task.status is TaskStatus.RUNNING
    assert result.decision.kind is DecisionKind.TOOL_CALL
    assert result.decision.tool_name == "search_code"
    assert len(snapshot.ticks) == 1
    assert [event.kind for event in snapshot.events] == [
        "task.created",
        "tick.started",
        "tool.called",
        "tool.succeeded",
        "tick.completed",
    ]
    completed = snapshot.events[-1]
    assert completed.payload["decision"] == {
        "kind": "tool_call",
        "reason_code": "DEMO_CODE_DIAGNOSIS",
        "tool_name": "search_code",
    }
    assert snapshot.artifacts[0].tool_name == "search_code"
    assert snapshot.evidence[0].source == "tool:search_code"
    assert snapshot.usage[0].route_reason == "DEMO_CODE_DIAGNOSIS"
    assert snapshot.usage[0].reserved_tokens >= snapshot.usage[0].actual_tokens


def test_advance_persists_budget_exhaustion_in_the_store_projection() -> None:
    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal="Diagnose a test failure",
        repository_root="/controlled/repository",
    )
    task.budget = task.budget.__class__(total_tokens=100, max_ticks=1)
    store.create_task(task)
    runtime = AgentRuntime(store=store)

    runtime.advance(task.task_id, lease_id="worker-a")
    result = runtime.advance(task.task_id, lease_id="worker-a")

    assert result.task.status is TaskStatus.BUDGET_EXHAUSTED
    assert store.snapshot(task.task_id).task.status is TaskStatus.BUDGET_EXHAUSTED


def test_context_projection_keeps_task_goal_and_injects_at_most_three_schemas() -> None:
    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal="Diagnose why the payment refund test fails",
        repository_root="/controlled/repository",
    )
    store.create_task(task)

    result = AgentRuntime(store=store).advance(task.task_id, lease_id="worker-a")

    assert result.context is not None
    assert result.context.payload["task"]["goal"] == task.goal
    schemas = result.context.payload["selected_tool_schemas"]
    assert len(schemas) == 3
    assert schemas[0]["name"] == "search_code"
    assert schemas[0]["input_schema"]["required"] == ["query"]
    assert result.context.estimated_input_tokens <= result.context.input_budget_tokens
