"""Redacted structured traces for legacy Agent executions."""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Mapping

from athena.learning.tracer import TraceEvent, Tracer
from athena.types import JSONValue

_SENSITIVE_KEY = re.compile(
    r"token|secret|password|authorization|cookie|api[_-]?key", re.I
)
_FORBIDDEN_KEY = re.compile(r"thought|prompt|raw[_-]?response|observation", re.I)
_MAX_TEXT_LENGTH = 512


@dataclass(frozen=True)
class DecisionTraceEvent:
    """A non-sensitive event describing a decision or its execution result."""

    run_id: str
    sequence: int
    event_type: str
    payload_redacted: dict[str, JSONValue]
    created_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class TaskTraceEvent:
    """Task-scoped projection used by the Phase 1 durable event store."""

    task_id: str
    tenant_id: str
    sequence: int
    event_type: str
    phase: str
    payload_redacted: dict[str, JSONValue]
    reason_code: str | None
    evidence_ids: tuple[str, ...]
    created_at: float
    trace_id: str


class StructuredTraceRecorder:
    """Record redacted legacy telemetry and project it into task events."""

    def __init__(self, tracer: Tracer | None = None) -> None:
        self._tracer = tracer
        self._events: dict[str, list[DecisionTraceEvent]] = {}

    def start_run(self) -> str:
        run_id = f"legacy-{uuid.uuid4().hex}"
        self._events[run_id] = []
        self.record(run_id, "agent.started", {"execution_profile": "legacy_react"})
        return run_id

    def record_llm_completed(
        self,
        run_id: str,
        *,
        step: int,
        model: str,
        usage: Mapping[str, int],
        latency_ms: int,
    ) -> None:
        self.record(
            run_id,
            "llm.completed",
            {
                "step": step,
                "model": model,
                "input_tokens": int(usage.get("prompt_tokens", 0)),
                "output_tokens": int(usage.get("completion_tokens", 0)),
                "latency_ms": latency_ms,
            },
        )

    def record_decision(
        self,
        run_id: str,
        *,
        step: int,
        action: str | None,
        arguments: Mapping[str, JSONValue],
    ) -> None:
        self.record(
            run_id,
            "decision.recorded",
            {
                "step": step,
                "action": action or "workflow.finish",
                "arguments": dict(arguments),
                "reason_code": "LEGACY_REACT_DECISION",
            },
        )

    def record_tool_started(self, run_id: str, *, step: int, action: str) -> None:
        self.record(run_id, "tool.started", {"step": step, "action": action})

    def record_tool_finished(
        self,
        run_id: str,
        *,
        step: int,
        action: str,
        success: bool,
        latency_ms: int,
        error_code: str | None = None,
    ) -> None:
        self.record(
            run_id,
            "tool.finished",
            {
                "step": step,
                "action": action,
                "result_status": "succeeded" if success else "failed",
                "latency_ms": latency_ms,
                "error_code": error_code,
            },
        )

    def finish_run(
        self, run_id: str, *, status: str, error_code: str | None = None
    ) -> None:
        self.record(
            run_id,
            "agent.finished",
            {"result_status": status, "error_code": error_code},
        )

    def record(
        self, run_id: str, event_type: str, payload: Mapping[str, JSONValue]
    ) -> None:
        events = self._events.get(run_id)
        if events is None:
            raise ValueError(f"unknown trace run: {run_id}")
        event = DecisionTraceEvent(
            run_id=run_id,
            sequence=len(events) + 1,
            event_type=event_type,
            payload_redacted=_redact_payload(payload),
        )
        events.append(event)
        if self._tracer is not None:
            self._tracer.record(
                TraceEvent(
                    name=event.event_type,
                    run_id=run_id,
                    timestamp=event.created_at,
                    payload={
                        "sequence": str(event.sequence),
                        "event_type": event.event_type,
                    },
                )
            )

    def events_for(self, run_id: str) -> tuple[DecisionTraceEvent, ...]:
        return tuple(self._events.get(run_id, ()))

    def project_task(
        self, run_id: str, *, task_id: str, tenant_id: str, trace_id: str = ""
    ) -> tuple[TaskTraceEvent, ...]:
        if not task_id.strip() or not tenant_id.strip():
            raise ValueError("task_id and tenant_id must be non-empty")
        result: list[TaskTraceEvent] = []
        for event in self.events_for(run_id):
            payload = event.payload_redacted
            reason_code = payload.get("reason_code")
            result.append(
                TaskTraceEvent(
                    task_id=task_id,
                    tenant_id=tenant_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    phase="analyze",
                    payload_redacted=payload,
                    reason_code=reason_code if isinstance(reason_code, str) else None,
                    evidence_ids=(),
                    created_at=event.created_at,
                    trace_id=trace_id,
                )
            )
        return tuple(result)


def _redact_payload(payload: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for raw_key, value in payload.items():
        key = str(raw_key)
        if _FORBIDDEN_KEY.search(key):
            continue
        if _SENSITIVE_KEY.search(key):
            result[key] = "[REDACTED]"
        else:
            result[key] = _redact_value(value)
    return result


def _redact_value(value: JSONValue) -> JSONValue:
    if isinstance(value, str):
        return value[:_MAX_TEXT_LENGTH]
    if isinstance(value, Mapping):
        return _redact_payload(value)
    if isinstance(value, tuple | list):
        return [_redact_value(item) for item in value]
    return value
