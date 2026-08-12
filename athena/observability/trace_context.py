"""W3C Trace Context helpers shared by API, Outbox and Worker processes."""

from __future__ import annotations

import contextvars
import re
import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Mapping

from athena.types import JSONValue

TRACEPARENT_HEADER = "traceparent"
_TRACEPARENT = contextvars.ContextVar("athena_traceparent", default="")
_TRACEPARENT_RE = re.compile(r"^[\da-f]{2}-[\da-f]{32}-[\da-f]{16}-[\da-f]{2}$")
_SENSITIVE_KEY = re.compile(
    r"token|secret|password|authorization|cookie|api[_-]?key|prompt|thought", re.I
)


@dataclass(frozen=True)
class TraceLinkage:
    traceparent: str
    trace_id: str
    tenant_id: str
    task_id: str | None = None
    run_id: str | None = None
    call_id: str | None = None


def new_traceparent() -> str:
    return f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


def resolve_traceparent(value: str | None) -> str:
    normalized = (value or "").strip().lower()
    return normalized if _TRACEPARENT_RE.fullmatch(normalized) else new_traceparent()


def trace_id_from_traceparent(value: str | None) -> str:
    traceparent = resolve_traceparent(value)
    return traceparent.split("-")[1]


def make_trace_headers(value: str | None = None) -> dict[str, str]:
    return {TRACEPARENT_HEADER: resolve_traceparent(value or get_traceparent())}


def link_trace(
    *,
    traceparent: str | None,
    tenant_id: str,
    task_id: str | None = None,
    run_id: str | None = None,
    call_id: str | None = None,
) -> TraceLinkage:
    resolved = resolve_traceparent(traceparent)
    return TraceLinkage(
        traceparent=resolved,
        trace_id=trace_id_from_traceparent(resolved),
        tenant_id=tenant_id,
        task_id=task_id,
        run_id=run_id,
        call_id=call_id,
    )


def redact_trace_payload(payload: Mapping[str, JSONValue]) -> dict[str, JSONValue]:
    redacted: dict[str, JSONValue] = {}
    for raw_key, value in payload.items():
        key = str(raw_key)
        if _SENSITIVE_KEY.search(key):
            redacted[key] = "[REDACTED]"
        elif isinstance(value, Mapping):
            redacted[key] = redact_trace_payload(value)
        elif isinstance(value, list | tuple):
            redacted[key] = [
                redact_trace_payload(item) if isinstance(item, Mapping) else item
                for item in value
            ]
        else:
            redacted[key] = value
    return redacted


def set_traceparent(value: str) -> contextvars.Token[str]:
    return _TRACEPARENT.set(value)


def get_traceparent() -> str:
    return _TRACEPARENT.get()


@contextmanager
def worker_span(
    traceparent: str | None, task_id: str, tenant_id: str
) -> Iterator[None]:
    """Resume a W3C parent context when OTel is available, with no-op fallback."""
    try:
        from opentelemetry import propagate, trace

        context = propagate.extract(
            {TRACEPARENT_HEADER: resolve_traceparent(traceparent)}
        )
        tracer = trace.get_tracer("athena.worker")
        with tracer.start_as_current_span(
            "athena.worker.task", context=context
        ) as span:
            span.set_attribute("athena.task_id", task_id)
            span.set_attribute("athena.tenant_id", tenant_id)
            yield
    except ImportError:
        yield
