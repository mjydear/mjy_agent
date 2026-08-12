"""Crash-window acceptance for the Runtime idempotency journal."""

from __future__ import annotations

from athena.runtime import AgentRuntime, AgentTask, InMemoryRuntimeStore, ReadOnlyToolCatalog


class _CountingTools(ReadOnlyToolCatalog):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def invoke(self, **kwargs):
        self.calls += 1
        return super().invoke(**kwargs)


class _CommitFailsOnce(InMemoryRuntimeStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def commit_tick(self, **kwargs):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated process crash after effect completion")
        return super().commit_tick(**kwargs)


def test_completed_effect_is_reused_after_aggregate_commit_failure() -> None:
    store = _CommitFailsOnce()
    tools = _CountingTools()
    task = AgentTask.create(goal="Diagnose the failing test", repository_root="/repo")
    store.create_task(task)
    runtime = AgentRuntime(store=store, tools=tools)

    try:
        runtime.advance(task.task_id, lease_id="worker-a")
    except RuntimeError as exc:
        assert "simulated process crash" in str(exc)
    else:
        raise AssertionError("the first aggregate commit should fail")

    result = runtime.advance(task.task_id, lease_id="worker-a")

    assert result.tick is not None
    assert tools.calls == 1
    assert len(store.snapshot(task.task_id).artifacts) == 1
