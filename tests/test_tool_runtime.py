"""Tests for the policy-workflow Tool V2 governance boundary."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest

from athena.agent.policy.contracts import (
    RiskLevel,
    ToolCallV2,
    ToolSpecV2,
    ToolStatus,
)
from athena.exceptions import ErrorCode, OpsError
from athena.tools import (
    AuditLogger,
    ToolAuditEvent,
    ToolRegistry,
    ToolRuntime,
    ToolRuntimeContext,
)


def _spec(*, readonly: bool = True, timeout: float = 1.0) -> ToolSpecV2:
    return ToolSpecV2(
        name="k8s.pod.list",
        version="1.0.0",
        domain="kubernetes",
        input_schema={
            "type": "object",
            "properties": {
                "namespace": {"type": "string"},
                "limit": {"type": "integer"},
                "api_token": {"type": "string"},
            },
            "required": ["namespace"],
            "additionalProperties": False,
        },
        output_schema={},
        required_capabilities=("k8s.workload.read",),
        risk_level=RiskLevel.S1,
        readonly=readonly,
        idempotent=True,
        timeout_seconds=timeout,
    )


def _context(**overrides: object) -> ToolRuntimeContext:
    values: dict[str, object] = {
        "tenant_id": "tenant-a",
        "environment_id": "env-prod",
        "allowed_capabilities": frozenset({"k8s.workload.read"}),
        "allowed_namespaces": frozenset({"payment"}),
    }
    values.update(overrides)
    return ToolRuntimeContext(**values)  # type: ignore[arg-type]


def _call(**overrides: object) -> ToolCallV2:
    values: dict[str, object] = {
        "call_id": "call-1",
        "task_id": "task-1",
        "tenant_id": "tenant-a",
        "tool_name": "k8s.pod.list",
        "arguments": {"namespace": "payment"},
    }
    values.update(overrides)
    return ToolCallV2(**values)  # type: ignore[arg-type]


def _runtime(registry: ToolRegistry, **kwargs: object) -> ToolRuntime:
    return ToolRuntime(
        registry,
        {"k8s.pod.list": _spec(**kwargs)},
        {"k8s.pod.list": "legacy_list"},
        retry_backoff_seconds=0,
    )


@pytest.mark.asyncio
async def test_runtime_rejects_unknown_tool_before_registry_invocation() -> None:
    registry = ToolRegistry()
    runtime = _runtime(registry)

    result = await runtime.invoke(_call(tool_name="unknown.tool"), _context())

    assert result.status is ToolStatus.REJECTED
    assert result.error_code == "TOOL_NOT_FOUND"


@pytest.mark.asyncio
async def test_runtime_validates_schema_capability_and_namespace() -> None:
    registry = ToolRegistry()
    runtime = _runtime(registry)

    missing = await runtime.invoke(_call(arguments={}), _context())
    invalid = await runtime.invoke(
        _call(arguments={"namespace": "payment", "limit": "10"}), _context()
    )
    forbidden = await runtime.invoke(
        _call(arguments={"namespace": "other"}), _context()
    )
    capability = await runtime.invoke(
        _call(), _context(allowed_capabilities=frozenset())
    )

    assert missing.error_code == "TOOL_ARGUMENT_REQUIRED"
    assert invalid.error_code == "TOOL_ARGUMENT_INVALID"
    assert forbidden.error_code == "OPS_NAMESPACE_FORBIDDEN"
    assert capability.error_code == "CAPABILITY_FORBIDDEN"


@pytest.mark.asyncio
async def test_runtime_normalizes_success_and_redacts_audit_arguments() -> None:
    registry = ToolRegistry()

    @registry.register
    def legacy_list(namespace: str, api_token: str = "") -> str:
        """A legacy K8s read adapter."""
        return '[{"name": "payment-api"}]'

    audit = AuditLogger()
    runtime = ToolRuntime(
        registry,
        {"k8s.pod.list": _spec()},
        {"k8s.pod.list": "legacy_list"},
        audit_logger=audit,
    )

    result = await runtime.invoke(
        _call(arguments={"namespace": "payment", "api_token": "secret-value"}),
        _context(),
    )

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == [{"name": "payment-api"}]
    assert "secret-value" not in audit.events[-1].detail
    assert "[REDACTED]" in audit.events[-1].detail


@pytest.mark.asyncio
async def test_runtime_enforces_timeout_and_normalizes_retryable_failure() -> None:
    registry = ToolRegistry()

    @registry.register
    async def legacy_list(namespace: str) -> str:
        """Slow legacy adapter."""
        await asyncio.sleep(0.05)
        return "[]"

    timeout_runtime = _runtime(registry, timeout=0.001)
    timed_out = await timeout_runtime.invoke(_call(), _context())
    assert timed_out.status is ToolStatus.TIMED_OUT
    assert timed_out.retryable is True

    attempts = 0

    @registry.register
    def legacy_list(namespace: str) -> str:
        """Unavailable legacy adapter."""
        nonlocal attempts
        attempts += 1
        raise RuntimeError("connection unavailable")

    failed = await _runtime(registry).invoke(_call(), _context())
    assert failed.status is ToolStatus.FAILED
    assert failed.retryable is True
    assert attempts == 3


@pytest.mark.asyncio
async def test_runtime_timeout_interrupts_blocking_sync_handler() -> None:
    registry = ToolRegistry()

    @registry.register
    def legacy_list(namespace: str) -> str:
        """Blocking legacy adapter."""
        time.sleep(0.15)
        return "[]"

    runtime = ToolRuntime(
        registry,
        {"k8s.pod.list": _spec(timeout=0.01)},
        {"k8s.pod.list": "legacy_list"},
        max_attempts=1,
    )
    started_at = time.perf_counter()

    result = await runtime.invoke(_call(), _context())
    elapsed = time.perf_counter() - started_at

    assert result.status is ToolStatus.TIMED_OUT
    assert result.error_code == "TOOL_TIMEOUT"
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_runtime_retries_transient_failure_until_success() -> None:
    registry = ToolRegistry()
    attempts = 0

    @registry.register
    def legacy_list(namespace: str) -> str:
        """Flaky legacy adapter."""
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionError("connection unavailable")
        return '[{"name": "payment-api"}]'

    result = await _runtime(registry).invoke(_call(), _context())

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == [{"name": "payment-api"}]
    assert attempts == 2


@pytest.mark.asyncio
async def test_runtime_does_not_retry_non_retryable_stable_error() -> None:
    registry = ToolRegistry()
    attempts = 0

    @registry.register
    def legacy_list(namespace: str) -> str:
        """Forbidden legacy adapter."""
        nonlocal attempts
        attempts += 1
        raise OpsError(ErrorCode.ENV_PERMISSION_DENIED, "provider access denied")

    result = await _runtime(registry).invoke(_call(), _context())

    assert result.status is ToolStatus.FAILED
    assert result.error_code == ErrorCode.ENV_PERMISSION_DENIED
    assert result.retryable is False
    assert attempts == 1


@pytest.mark.asyncio
async def test_runtime_externalizes_large_result_after_evidence_persistence() -> None:
    registry = ToolRegistry()
    payload = {"items": [{"name": "payment-api", "log": "x" * 256}]}

    @registry.register
    def legacy_list(namespace: str) -> str:
        """Large legacy adapter result."""
        return json.dumps(payload)

    class RecordingEvidenceSink:
        def __init__(self) -> None:
            self.data: object = None

        def persist(
            self, call: ToolCallV2, spec: ToolSpecV2, data: object
        ) -> tuple[str, ...]:
            self.data = data
            return ("evidence-large-result",)

    evidence = RecordingEvidenceSink()
    runtime = ToolRuntime(
        registry,
        {"k8s.pod.list": _spec()},
        {"k8s.pod.list": "legacy_list"},
        evidence_sink=evidence,  # type: ignore[arg-type]
        max_inline_result_bytes=64,
    )

    result = await runtime.invoke(_call(), _context())

    assert evidence.data == payload
    assert result.status is ToolStatus.SUCCEEDED
    assert result.data is None
    assert result.evidence_refs == ("evidence-large-result",)
    assert "stored as evidence" in result.summary
    assert "x" * 64 not in result.summary


@pytest.mark.asyncio
async def test_readonly_audit_failure_does_not_replace_execution_result(
    caplog: pytest.LogCaptureFixture,
) -> None:
    registry = ToolRegistry()

    @registry.register
    def legacy_list(namespace: str) -> str:
        """Successful legacy adapter."""
        return "[]"

    class FailingFinishedAudit(AuditLogger):
        def __init__(self) -> None:
            super().__init__()
            self.record_attempts = 0

        def record(self, event: ToolAuditEvent) -> None:
            self.record_attempts += 1
            if event.action == "finished":
                raise OSError("audit sink unavailable")
            super().record(event)

    audit = FailingFinishedAudit()
    runtime = ToolRuntime(
        registry,
        {"k8s.pod.list": _spec()},
        {"k8s.pod.list": "legacy_list"},
        audit_logger=audit,
    )

    with caplog.at_level(logging.WARNING, logger="athena.tools.runtime"):
        result = await runtime.invoke(_call(), _context())

    assert result.status is ToolStatus.SUCCEEDED
    assert result.data == []
    assert audit.record_attempts == 2
    assert "Readonly tool audit failed" in caplog.text


@pytest.mark.asyncio
async def test_runtime_rejects_write_spec_under_readonly_context() -> None:
    registry = ToolRegistry()
    runtime = _runtime(registry, readonly=False)

    result = await runtime.invoke(_call(), _context())

    assert result.status is ToolStatus.REJECTED
    assert result.error_code == "WRITE_OPERATION_FORBIDDEN"
