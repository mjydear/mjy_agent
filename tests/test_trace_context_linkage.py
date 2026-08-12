"""TraceContext propagation and redaction tests across queue boundaries."""

from __future__ import annotations

import pytest

from athena.infra.task_stream import InMemoryTaskStream
from athena.observability.trace_context import (
    link_trace,
    make_trace_headers,
    new_traceparent,
    redact_trace_payload,
    resolve_traceparent,
    trace_id_from_traceparent,
)


def test_traceparent_is_w3c_normalized_and_headers_are_reusable() -> None:
    traceparent = new_traceparent()
    headers = make_trace_headers(traceparent.upper())

    assert headers["traceparent"] == traceparent
    assert trace_id_from_traceparent(traceparent) == traceparent.split("-")[1]
    assert resolve_traceparent("not-valid") != "not-valid"


def test_trace_payload_redaction_removes_prompt_secret_and_token_material() -> None:
    payload = redact_trace_payload(
        {
            "task_id": "task-1",
            "raw_prompt": "do hidden work",
            "headers": {"authorization": "Bearer secret-token"},
            "items": [{"api_key": "sk-123", "summary": "safe"}],
        }
    )

    assert payload["task_id"] == "task-1"
    assert payload["raw_prompt"] == "[REDACTED]"
    assert payload["headers"] == {"authorization": "[REDACTED]"}
    assert payload["items"] == [{"api_key": "[REDACTED]", "summary": "safe"}]


@pytest.mark.asyncio
async def test_traceparent_survives_stream_boundary_without_raw_payload() -> None:
    traceparent = new_traceparent()
    stream = InMemoryTaskStream()

    message_id = await stream.publish("task-1", "tenant-a", traceparent)
    (message,) = await stream.consume("worker-a", count=1, block_ms=0)
    linkage = link_trace(
        traceparent=message.traceparent,
        tenant_id=message.tenant_id,
        task_id=message.task_id,
        run_id=message_id,
    )

    assert linkage.traceparent == traceparent
    assert linkage.trace_id == traceparent.split("-")[1]
    assert linkage.tenant_id == "tenant-a"
    assert linkage.task_id == "task-1"
