"""Application service for governed Episodic and Semantic Memory writes."""

from __future__ import annotations

from typing import Any

from athena.runtime.learning import TrajectorySummaryBuilder
from athena.runtime.memory import (
    EpisodicMemoryProjector,
    SemanticMemory,
)


class RuntimeLongTermMemoryService:
    """Project completed Runtime facts without exposing raw artifacts."""

    def __init__(
        self,
        store: Any,
        *,
        episodic_memory: Any | None = None,
        semantic_memory: Any | None = None,
    ) -> None:
        self._store = store
        self._episodic_memory = episodic_memory
        self._semantic_memory = semantic_memory
        self._trajectory_builder = TrajectorySummaryBuilder()
        self._episodic_projector = EpisodicMemoryProjector()

    def capture_episodic(self, task_id: str, *, tenant_id: str):
        if self._episodic_memory is None:
            return None
        trajectory = self._trajectory_builder.build(
            self._store.snapshot(task_id), tenant_id=tenant_id
        )
        memory = self._episodic_projector.from_trajectory(trajectory)
        if memory is not None:
            self._episodic_memory.upsert(memory)
        return memory

    def submit_semantic(self, memory: SemanticMemory) -> None:
        if self._semantic_memory is None:
            raise RuntimeError("semantic memory is unavailable")
        if memory.state.value != "candidate":
            raise ValueError("semantic memory must enter through candidate state")
        self._semantic_memory.submit(memory)

    def approve_semantic(
        self, memory_id: str, *, tenant_id: str, reviewed_by: str
    ) -> None:
        if self._semantic_memory is None:
            raise RuntimeError("semantic memory is unavailable")
        if not reviewed_by.strip():
            raise ValueError("reviewed_by must be non-empty")
        self._semantic_memory.approve(
            memory_id, tenant_id=tenant_id, reviewed_by=reviewed_by
        )


__all__ = ["RuntimeLongTermMemoryService"]
