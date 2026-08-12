"""Lease-aware durable task worker with ACK-after-checkpoint semantics."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from athena.api.repositories import PersistedTask, TaskRepository
from athena.api.repositories.task_repository import TaskLeaseLostError
from athena.infra.task_stream import TaskStream
from athena.observability.trace_context import worker_span


@dataclass(frozen=True)
class WorkerOutcome:
    state: dict[str, object]
    phase: str = "report"
    status: str = "succeeded"
    event_type: str = "task.completed"
    retry_delay_seconds: float | None = None
    error_code: str | None = None


class TaskHandler(Protocol):
    def __call__(self, task: PersistedTask) -> Awaitable[WorkerOutcome]: ...


class DurableTaskWorker:
    """Consumes task references and only ACKs after a durable state transition."""

    def __init__(
        self,
        tasks: TaskRepository,
        stream: TaskStream,
        handler: TaskHandler,
        *,
        worker_id: str,
        lease_ttl_seconds: int,
        max_attempts: int,
    ) -> None:
        self._tasks = tasks
        self._stream = stream
        self._handler = handler
        self._worker_id = worker_id
        self._lease_ttl_seconds = lease_ttl_seconds
        self._max_attempts = max_attempts

    async def run_once(self, *, count: int, block_ms: int, reclaim_idle_ms: int) -> int:
        reclaimed = await self._stream.reclaim(
            self._worker_id, min_idle_ms=reclaim_idle_ms, count=count
        )
        messages = reclaimed or await self._stream.consume(
            self._worker_id, count=count, block_ms=block_ms
        )
        processed = 0
        for message in messages:
            with worker_span(message.traceparent, message.task_id, message.tenant_id):
                processed += await self._process_message(message)
        return processed

    async def _process_message(self, message) -> int:
        claim = await self._tasks.claim_task(
            message.tenant_id,
            message.task_id,
            self._worker_id,
            self._lease_ttl_seconds,
        )
        if claim is None:
            await self._stream.ack(message.message_id)
            return 0
        try:
            outcome = await self._handler(claim)
            if outcome.retry_delay_seconds is not None:
                error_code = outcome.error_code or "TASK_RETRY_REQUESTED"
                if claim.attempt_count >= self._max_attempts:
                    await self._tasks.checkpoint(
                        claim.tenant_id,
                        claim.task_id,
                        worker_id=self._worker_id,
                        expected_state_version=claim.state_version,
                        lease_generation=claim.lease_generation,
                        state={"error_code": error_code},
                        phase="report",
                        status="failed",
                        event_type="task.failed",
                        event_data={"error_code": error_code},
                    )
                    await self._stream.dead_letter(message, error_code)
                else:
                    await self._tasks.requeue(
                        claim.tenant_id,
                        claim.task_id,
                        worker_id=self._worker_id,
                        expected_state_version=claim.state_version,
                        lease_generation=claim.lease_generation,
                        delay_seconds=outcome.retry_delay_seconds,
                        error_code=error_code,
                    )
                    await self._stream.ack(message.message_id)
            else:
                await self._tasks.checkpoint(
                    claim.tenant_id,
                    claim.task_id,
                    worker_id=self._worker_id,
                    expected_state_version=claim.state_version,
                    lease_generation=claim.lease_generation,
                    state=outcome.state,
                    phase=outcome.phase,
                    status=outcome.status,
                    event_type=outcome.event_type,
                    event_data={"worker_id": self._worker_id},
                )
                await self._stream.ack(message.message_id)
            return 1
        except TaskLeaseLostError:
            # Cancellation or a newer lease won the race. Its durable state is authoritative.
            await self._stream.ack(message.message_id)
            return 0
        except Exception as exc:  # noqa: BLE001 - preserve the message for reclaim
            if claim.attempt_count >= self._max_attempts:
                await self._tasks.checkpoint(
                    claim.tenant_id,
                    claim.task_id,
                    worker_id=self._worker_id,
                    expected_state_version=claim.state_version,
                    lease_generation=claim.lease_generation,
                    state={"error_code": "WORKER_HANDLER_FAILED"},
                    phase="report",
                    status="failed",
                    event_type="task.failed",
                    event_data={"error_code": "WORKER_HANDLER_FAILED"},
                )
                await self._stream.dead_letter(message, str(exc))
            else:
                await self._tasks.requeue(
                    claim.tenant_id,
                    claim.task_id,
                    worker_id=self._worker_id,
                    expected_state_version=claim.state_version,
                    lease_generation=claim.lease_generation,
                    delay_seconds=min(60.0, 2 ** min(claim.attempt_count, 6)),
                    error_code="WORKER_HANDLER_FAILED",
                )
                await self._stream.ack(message.message_id)
            return 0
