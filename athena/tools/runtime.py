"""Governed Tool V2 execution for policy workflows.

The legacy :class:`ToolRegistry` remains the compatibility surface for ReAct.
This module is the only supported execution boundary for the new policy workflow:
it resolves a stable V2 action, applies deterministic policy checks, then invokes
the legacy adapter behind a timeout.  It deliberately contains no model logic.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

from athena.agent.policy.contracts import (
    RiskLevel,
    ToolCallV2,
    ToolResultV2,
    ToolSpecV2,
    ToolStatus,
)
from athena.tools.audit import AuditLogger, ToolAuditEvent
from athena.tools.registry import ToolCall, ToolRegistry, ToolResult
from athena.types import JSONValue

logger = logging.getLogger(__name__)

_DEFAULT_MAX_ATTEMPTS = 3
_DEFAULT_RETRY_BACKOFF_SECONDS = 0.05
_DEFAULT_MAX_INLINE_RESULT_BYTES = 64 * 1024

_RISK_ORDER = {
    RiskLevel.S0: 0,
    RiskLevel.S1: 1,
    RiskLevel.S2: 2,
    RiskLevel.S3: 3,
    RiskLevel.S4: 4,
    RiskLevel.S5: 5,
}
_SENSITIVE_ARGUMENT_MARKERS = (
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
    "api_key",
)
_NON_RETRYABLE_ERROR_CODES = frozenset(
    {
        "CAPABILITY_FORBIDDEN",
        "ENV_DATA_ORIGIN_FORBIDDEN",
        "ENV_PERMISSION_DENIED",
        "ENV_SCOPE_DENIED",
        "OPS_NAMESPACE_FORBIDDEN",
        "RISK_LEVEL_FORBIDDEN",
        "TOOL_ARGUMENT_INVALID",
        "TOOL_ARGUMENT_REQUIRED",
        "TOOL_ARGUMENT_UNKNOWN",
        "TOOL_NOT_ALLOWED",
        "TOOL_NOT_FOUND",
        "WRITE_OPERATION_FORBIDDEN",
    }
)
_RETRYABLE_ERROR_CODES = frozenset(
    {
        "ENV_CONNECTION_FAILED",
        "ENV_METRICS_UNAVAILABLE",
        "ENV_TIMEOUT",
        "TOOL_TIMEOUT",
    }
)
_RETRYABLE_ERROR_MARKERS = (
    "timeout",
    "timed out",
    "connection",
    "temporar",
    "rate limit",
    "too many requests",
    "unavailable",
    "502",
    "503",
    "504",
    "econnreset",
    "reset by peer",
)


class EvidenceSink(Protocol):
    """Optional Phase 1 seam; durable Evidence storage is added by P1-02."""

    def persist(
        self, call: ToolCallV2, spec: ToolSpecV2, data: JSONValue | None
    ) -> tuple[str, ...] | Awaitable[tuple[str, ...]]: ...


@dataclass(frozen=True)
class ToolRuntimeContext:
    """Server-derived, task-scoped permissions for one Tool V2 invocation."""

    tenant_id: str
    environment_id: str
    allowed_capabilities: frozenset[str] = field(default_factory=frozenset)
    allowed_tool_names: frozenset[str] = field(default_factory=frozenset)
    allowed_namespaces: frozenset[str] = field(default_factory=frozenset)
    max_risk_level: RiskLevel = RiskLevel.S1
    readonly_only: bool = True

    def __post_init__(self) -> None:
        if not self.tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        if not self.environment_id.strip():
            raise ValueError("environment_id must be a non-empty string")


class ToolRuntime:
    """Apply deterministic governance before invoking a registered provider tool.

    Phase 1 readonly audit writes are fail-open and logged. A write-capable tool
    must record its ``started`` audit event successfully before execution.
    """

    def __init__(
        self,
        registry: ToolRegistry,
        specs: Mapping[str, ToolSpecV2],
        legacy_tool_names: Mapping[str, str],
        *,
        audit_logger: AuditLogger | None = None,
        evidence_sink: EvidenceSink | None = None,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        retry_backoff_seconds: float = _DEFAULT_RETRY_BACKOFF_SECONDS,
        max_inline_result_bytes: int = _DEFAULT_MAX_INLINE_RESULT_BYTES,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if retry_backoff_seconds < 0:
            raise ValueError("retry_backoff_seconds must not be negative")
        if max_inline_result_bytes < 1:
            raise ValueError("max_inline_result_bytes must be positive")
        self._registry = registry
        self._specs = dict(specs)
        self._legacy_tool_names = dict(legacy_tool_names)
        self._audit_logger = audit_logger
        self._evidence_sink = evidence_sink
        self._max_attempts = max_attempts
        self._retry_backoff_seconds = retry_backoff_seconds
        self._max_inline_result_bytes = max_inline_result_bytes

        missing = set(self._specs) - set(self._legacy_tool_names)
        if missing:
            raise ValueError(f"missing legacy adapter(s): {sorted(missing)}")

    async def invoke(
        self, call: ToolCallV2, context: ToolRuntimeContext
    ) -> ToolResultV2:
        """Execute exactly one V2 tool call after tenant and policy validation."""
        if call.tenant_id != context.tenant_id:
            return self._rejected(
                call, "TENANT_MISMATCH", "tool call tenant is invalid"
            )

        spec = self._specs.get(call.tool_name)
        if spec is None:
            return self._rejected(
                call, "TOOL_NOT_FOUND", "requested tool is unavailable"
            )

        rejection = self._validate_call(spec, call, context)
        if rejection is not None:
            return self._rejected(call, rejection, "tool call was rejected by policy")

        legacy_name = self._legacy_tool_names[call.tool_name]
        self._audit(
            call,
            "started",
            True,
            tool_name=spec.name,
            fail_open=spec.readonly,
        )
        legacy_result = await self._execute_with_retry(call, spec, legacy_name)
        if legacy_result is None:
            result = ToolResultV2(
                status=ToolStatus.TIMED_OUT,
                summary="tool execution timed out",
                data=None,
                error_code="TOOL_TIMEOUT",
                retryable=spec.idempotent,
            )
            self._audit(
                call,
                "finished",
                False,
                tool_name=spec.name,
                error="TOOL_TIMEOUT",
                fail_open=True,
            )
            return result

        if not legacy_result.success:
            error_code = self._error_code(legacy_result.error)
            retryable = spec.idempotent and self._is_retryable(legacy_result.error)
            result = ToolResultV2(
                status=ToolStatus.FAILED,
                summary="tool execution failed",
                data=None,
                error_code=error_code,
                retryable=retryable,
            )
            self._audit(
                call,
                "finished",
                False,
                tool_name=spec.name,
                error=error_code,
                fail_open=True,
            )
            return result

        data = self._decode_result(legacy_result.content)
        evidence_refs = await self._persist_evidence(call, spec, data)
        summary = f"{spec.name} completed"
        inline_data = data
        result_bytes = self._result_size_bytes(data)
        if evidence_refs and result_bytes > self._max_inline_result_bytes:
            inline_data = None
            summary = (
                f"{spec.name} completed; full result stored as evidence "
                f"({result_bytes} bytes)"
            )
        result = ToolResultV2(
            status=ToolStatus.SUCCEEDED,
            summary=summary,
            data=inline_data,
            evidence_refs=evidence_refs,
        )
        self._audit(
            call,
            "finished",
            True,
            tool_name=spec.name,
            fail_open=True,
        )
        return result

    async def _execute_with_retry(
        self, call: ToolCallV2, spec: ToolSpecV2, legacy_name: str
    ) -> ToolResult | None:
        """Return the final legacy result, or ``None`` after a final timeout."""
        for attempt in range(1, self._max_attempts + 1):
            try:
                result = await asyncio.wait_for(
                    self._registry.invoke(
                        ToolCall(name=legacy_name, arguments=dict(call.arguments))
                    ),
                    timeout=spec.timeout_seconds,
                )
            except TimeoutError:
                if not self._can_retry(spec, attempt, retryable=True):
                    return None
                await self._wait_before_retry(spec.name, attempt, "TOOL_TIMEOUT")
                continue

            retryable = self._is_retryable(result.error)
            if result.success or not self._can_retry(spec, attempt, retryable):
                return result
            await self._wait_before_retry(
                spec.name, attempt, self._error_code(result.error)
            )
        raise AssertionError("tool retry loop exhausted without a result")

    def _can_retry(self, spec: ToolSpecV2, attempt: int, retryable: bool) -> bool:
        return retryable and spec.idempotent and attempt < self._max_attempts

    async def _wait_before_retry(
        self, tool_name: str, attempt: int, reason: str
    ) -> None:
        delay = self._retry_backoff_seconds * (2 ** (attempt - 1))
        logger.warning(
            "Retrying tool %s after attempt %d/%d (%s)",
            tool_name,
            attempt,
            self._max_attempts,
            reason,
        )
        await asyncio.sleep(delay)

    def _validate_call(
        self, spec: ToolSpecV2, call: ToolCallV2, context: ToolRuntimeContext
    ) -> str | None:
        if context.allowed_tool_names and spec.name not in context.allowed_tool_names:
            return "TOOL_NOT_ALLOWED"
        if context.readonly_only and not spec.readonly:
            return "WRITE_OPERATION_FORBIDDEN"
        if _RISK_ORDER[spec.risk_level] > _RISK_ORDER[context.max_risk_level]:
            return "RISK_LEVEL_FORBIDDEN"
        if not set(spec.required_capabilities).issubset(context.allowed_capabilities):
            return "CAPABILITY_FORBIDDEN"
        schema_error = self._validate_schema(spec.input_schema, call.arguments)
        if schema_error is not None:
            return schema_error
        namespace = call.arguments.get("namespace")
        if context.allowed_namespaces and (
            not isinstance(namespace, str)
            or namespace not in context.allowed_namespaces
        ):
            return "OPS_NAMESPACE_FORBIDDEN"
        return None

    @staticmethod
    def _validate_schema(
        schema: Mapping[str, JSONValue], arguments: Mapping[str, JSONValue]
    ) -> str | None:
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [
                name
                for name in required
                if isinstance(name, str) and name not in arguments
            ]
            if missing:
                return "TOOL_ARGUMENT_REQUIRED"

        properties = schema.get("properties")
        if not isinstance(properties, Mapping):
            return None
        if schema.get("additionalProperties") is False:
            unknown = set(arguments) - set(properties)
            if unknown:
                return "TOOL_ARGUMENT_UNKNOWN"
        for name, value in arguments.items():
            definition = properties.get(name)
            if not isinstance(definition, Mapping):
                continue
            expected = definition.get("type")
            if isinstance(expected, str) and not ToolRuntime._matches_type(
                value, expected
            ):
                return "TOOL_ARGUMENT_INVALID"
        return None

    @staticmethod
    def _matches_type(value: JSONValue, expected: str) -> bool:
        if expected == "string":
            return isinstance(value, str)
        if expected == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if expected == "number":
            return isinstance(value, (int, float)) and not isinstance(value, bool)
        if expected == "boolean":
            return isinstance(value, bool)
        if expected == "object":
            return isinstance(value, dict)
        if expected == "array":
            return isinstance(value, list)
        if expected == "null":
            return value is None
        return True

    async def _persist_evidence(
        self, call: ToolCallV2, spec: ToolSpecV2, data: JSONValue | None
    ) -> tuple[str, ...]:
        if self._evidence_sink is None:
            return ()
        persisted = self._evidence_sink.persist(call, spec, data)
        if inspect.isawaitable(persisted):
            persisted = await persisted
        return tuple(str(reference) for reference in persisted)

    def _rejected(
        self, call: ToolCallV2, error_code: str, summary: str
    ) -> ToolResultV2:
        # A failed audit sink must never turn a deterministic rejection into an
        # executable call or replace its stable policy error.
        self._audit(call, "rejected", False, error=error_code, fail_open=True)
        return ToolResultV2(
            status=ToolStatus.REJECTED,
            summary=summary,
            data=None,
            error_code=error_code,
        )

    def _audit(
        self,
        call: ToolCallV2,
        action: str,
        success: bool,
        *,
        tool_name: str | None = None,
        error: str | None = None,
        fail_open: bool,
    ) -> None:
        if self._audit_logger is None:
            return
        detail: dict[str, JSONValue] = {
            "call_id": call.call_id,
            "task_id": call.task_id,
            "tenant_id": call.tenant_id,
            "arguments": self._redact(call.arguments),
        }
        if error:
            detail["error_code"] = error
        try:
            self._audit_logger.record(
                ToolAuditEvent(
                    tool_name=tool_name or call.tool_name,
                    action=action,
                    success=success,
                    actor=f"tenant:{call.tenant_id}",
                    detail=json.dumps(detail, ensure_ascii=True, sort_keys=True),
                )
            )
        except Exception as exc:
            if not fail_open:
                raise RuntimeError("tool audit is unavailable") from exc
            # Phase 1 executes readonly calls. Their audit projection is
            # best-effort: a sink outage is observable but cannot alter a
            # rejection or an already established execution result.
            logger.warning(
                "Readonly tool audit failed for %s action=%s",
                tool_name or call.tool_name,
                action,
                exc_info=True,
            )

    @staticmethod
    def _redact(value: JSONValue) -> JSONValue:
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED]"
                    if any(
                        marker in key.lower() for marker in _SENSITIVE_ARGUMENT_MARKERS
                    )
                    else ToolRuntime._redact(item)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [ToolRuntime._redact(item) for item in value]
        return value

    @staticmethod
    def _decode_result(content: str) -> JSONValue:
        try:
            return json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return content

    @staticmethod
    def _result_size_bytes(data: JSONValue | None) -> int:
        return len(
            json.dumps(
                data,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        )

    @staticmethod
    def _error_code(error: str | None) -> str:
        if not error:
            return "TOOL_EXECUTION_FAILED"
        return error.split(":", maxsplit=1)[0].strip() or "TOOL_EXECUTION_FAILED"

    @staticmethod
    def _is_retryable(error: str | None) -> bool:
        error_code = ToolRuntime._error_code(error)
        if error_code in _NON_RETRYABLE_ERROR_CODES:
            return False
        if error_code in _RETRYABLE_ERROR_CODES:
            return True
        value = (error or "").lower()
        return any(marker in value for marker in _RETRYABLE_ERROR_MARKERS)
