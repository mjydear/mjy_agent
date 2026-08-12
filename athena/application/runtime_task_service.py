"""Application module for the public Agent Runtime task contract."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from athena.application.runtime_worker import RuntimeWorker
from athena.runtime import (
    AgentRuntime,
    AgentTask,
    InMemoryRuntimeStore,
    RuntimeSnapshot,
    TaskProfile,
    TaskStatus,
)


class RuntimeTaskService:
    """Keep HTTP routes away from Runtime aggregate mutation details."""

    def __init__(
        self,
        runtime: AgentRuntime,
        store: Any,
        *,
        backend: str = "memory-demo",
        decision_mode: str = "deterministic-demo",
        memory_strategy: str = "p0-bounded-context",
        worker: RuntimeWorker | None = None,
    ) -> None:
        self._runtime = runtime
        self._store = store
        self._worker = worker or RuntimeWorker(runtime)
        self._backend = backend
        self._decision_mode = decision_mode
        self._memory_strategy = memory_strategy

    def create(self, *, goal: str, repository_path: str, profile: str | None) -> dict[str, object]:
        root = Path(repository_path).expanduser().resolve()
        if not root.is_dir():
            raise ValueError("REPOSITORY_PATH_INVALID")
        try:
            task_profile = TaskProfile((profile or TaskProfile.STANDARD.value).lower())
        except ValueError as exc:
            raise ValueError("TASK_PROFILE_INVALID") from exc
        task = AgentTask.create(
            goal=goal,
            repository_root=str(root),
            profile=task_profile,
        )
        self._store.create_task(task)
        return self.task_view(self._store.snapshot(task.task_id))

    def list(self) -> dict[str, object]:
        return {
            "items": [self.task_summary(task) for task in self._store.list_tasks()],
        }

    def detail(self, task_id: str) -> dict[str, object]:
        return self.task_view(self._store.snapshot(task_id))

    def run(self, task_id: str) -> dict[str, object]:
        before = self._store.snapshot(task_id)
        if not before.task.status.terminal and before.task.status is not TaskStatus.WAITING_HUMAN:
            self._worker.run_to_boundary(
                task_id,
                max_ticks=min(12, before.task.budget.max_ticks),
            )
        return self.task_view(self._store.snapshot(task_id))

    def supply_human_input(self, task_id: str, value: str) -> dict[str, object]:
        self._store.supply_human_input(task_id, value)
        return self.run(task_id)

    def cancel(self, task_id: str) -> dict[str, object]:
        self._store.cancel_task(task_id)
        return self.task_view(self._store.snapshot(task_id))

    def events(self, task_id: str, after: int = 0) -> dict[str, object]:
        snapshot = self._store.snapshot(task_id)
        items = [
            {
                "id": event.event_id,
                "sequence": event.sequence,
                "type": event.kind,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }
            for event in snapshot.events
            if event.sequence > after
        ]
        return {"items": items, "next_cursor": items[-1]["sequence"] if items else after}

    def evidence(self, task_id: str) -> dict[str, object]:
        snapshot = self._store.snapshot(task_id)
        return {
            "items": [
                {
                    "id": item.evidence_id,
                    "artifact_id": item.artifact_id,
                    "source": item.source,
                    "summary": item.summary,
                    "created_at": item.created_at.isoformat(),
                }
                for item in snapshot.evidence
            ]
        }

    def context(self, task_id: str) -> dict[str, object]:
        snapshot = self._store.snapshot(task_id)
        context = snapshot.context
        compiled = context.payload if context is not None else {}
        working_memory = compiled.get("working_memory", {})
        running_summary = compiled.get("running_summary", snapshot.working_state.running_summary)
        governance = compiled.get("memory_governance", {})
        return {
            "snapshot": {
                "task_frame": {
                    "goal": snapshot.task.goal,
                    "repository_path": snapshot.task.repository_root,
                    "profile": snapshot.task.profile.value,
                },
                "pinned_evidence": list(snapshot.working_state.evidence_ids),
                "running_summary": running_summary,
                "selected_tool_schemas": list(
                    context.tool_schemas if context is not None else compiled.get("selected_tool_schemas", [])
                ),
                "recent_events": compiled.get("recent_events", []),
                "memory_references": {
                    "evidence": compiled.get("evidence_memory", compiled.get("evidence", [])),
                    "skills": compiled.get("skill_memory", []),
                    "unresolved_tool_pairs": working_memory.get("unresolved_tool_pairs", []),
                },
            },
            "metrics": {
                "estimated_input_tokens": context.estimated_input_tokens if context else 0,
                "input_budget_tokens": context.input_budget_tokens if context else 0,
                "omitted_event_count": context.omitted_event_count if context else 0,
                "compaction_count": max(
                    snapshot.working_state.compaction_count,
                    context.compaction_count if context else 0,
                ),
                "memory_governance": governance,
            },
        }

    def usage(self, task_id: str) -> dict[str, object]:
        snapshot = self._store.snapshot(task_id)
        return {
            "items": [
                {
                    "id": item.usage_id,
                    "tick_id": item.tick_id,
                    "purpose": item.purpose,
                    "model": item.model_tier,
                    "route_reason": item.route_reason,
                    "estimated_input_tokens": item.estimated_input_tokens,
                    "reserved_tokens": item.reserved_tokens,
                    "actual_tokens": item.actual_tokens,
                    "budget_mode": item.budget_mode,
                    "created_at": item.created_at.isoformat(),
                }
                for item in snapshot.usage
            ]
        }

    def task_view(self, snapshot: RuntimeSnapshot) -> dict[str, object]:
        task = self.task_summary(snapshot.task)
        task.update(
            {
                "tick_count": len(snapshot.ticks),
                "budget": {
                    "total_tokens": snapshot.task.budget.total_tokens,
                    "consumed_tokens": snapshot.task.budget.consumed_tokens,
                    "remaining_tokens": snapshot.task.budget.remaining_tokens,
                    "mode": snapshot.task.budget.mode,
                },
                "execution": {
                    "backend": self._backend,
                    "decision_mode": self._decision_mode,
                    "memory_strategy": self._memory_strategy,
                    "worker_id": self._worker.worker_id,
                },
            }
        )
        if snapshot.task.final_report is not None:
            task["report"] = {
                "root_cause": snapshot.task.final_report.root_cause,
                "repair_recommendation": snapshot.task.final_report.repair_recommendation,
                "evidence_ids": list(snapshot.task.final_report.evidence_ids),
            }
        return task

    @staticmethod
    def task_summary(task: AgentTask) -> dict[str, object]:
        return {
            "id": task.task_id,
            "goal": task.goal,
            "repository_path": task.repository_root,
            "profile": task.profile.value,
            "status": task.status.value,
            "budget_mode": task.budget.mode,
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
        }
