"""Bounded worker boundary for the synchronous Agent Runtime aggregate."""

from __future__ import annotations

import socket
from dataclasses import dataclass, field
from uuid import uuid4

from athena.runtime import AdvanceResult, AgentRuntime, TaskStatus


@dataclass
class RuntimeWorker:
    """Own a stable lease identity and invoke exactly one Runtime Tick at a time."""

    runtime: AgentRuntime
    worker_id: str = field(
        default_factory=lambda: f"runtime-worker-{socket.gethostname()}-{uuid4().hex[:12]}"
    )

    def advance_one(self, task_id: str, *, resume_input: str | None = None) -> AdvanceResult:
        return self.runtime.advance(
            task_id,
            lease_id=f"{self.worker_id}:{task_id}",
            resume_input=resume_input,
        )

    def run_to_boundary(self, task_id: str, *, max_ticks: int) -> AdvanceResult | None:
        """Run a finite batch; terminal and operator-input boundaries stop early."""

        if max_ticks < 1:
            raise ValueError("max_ticks must be positive")
        latest: AdvanceResult | None = None
        for _ in range(max_ticks):
            latest = self.advance_one(task_id)
            if (
                latest.tick is None
                or latest.task.status.terminal
                or latest.task.status is TaskStatus.WAITING_HUMAN
            ):
                return latest
        return latest
