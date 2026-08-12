"""Runtime adapter for governed ToolRuntime V2 invocations.

The model can choose a tool from the server-selected directory and provide
business arguments.  Identity, repository scope, permissions, call IDs, and
any injected arguments remain server-owned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from athena.agent.policy.contracts import (
    RiskLevel,
    ToolCallV2,
    ToolResultV2,
    ToolSpecV2,
    ToolStatus,
)
from athena.tools.runtime import ToolRuntime, ToolRuntimeContext
from athena.types import JSONValue

from .models import Decision, DecisionKind

_MAX_TOOL_SCHEMAS = 3
_SERVER_CONTROLLED_ARGUMENTS = frozenset(
    {
        "call_id",
        "environment_id",
        "lease_id",
        "permission_scope",
        "repository_path",
        "repository_root",
        "task_id",
        "tenant_id",
    }
)
_PUBLIC_REASON_CODES = frozenset(
    {
        "CAPABILITY_FORBIDDEN",
        "ENV_DATA_ORIGIN_FORBIDDEN",
        "ENV_PERMISSION_DENIED",
        "ENV_SCOPE_DENIED",
        "OPS_NAMESPACE_FORBIDDEN",
        "PATH_OUT_OF_SCOPE",
        "REPOSITORY_FILE_NOT_FOUND",
        "RISK_LEVEL_FORBIDDEN",
        "TEST_TARGET_FORBIDDEN",
        "TOOL_ARGUMENT_INVALID",
        "TOOL_ARGUMENT_REQUIRED",
        "TOOL_ARGUMENT_UNKNOWN",
        "TOOL_NOT_ALLOWED",
        "TOOL_NOT_FOUND",
        "WRITE_OPERATION_FORBIDDEN",
    }
)


class GovernedToolRuntime(Protocol):
    async def invoke(
        self, call: ToolCallV2, context: ToolRuntimeContext
    ) -> ToolResultV2: ...


@dataclass(frozen=True)
class RuntimeToolContext:
    """Server-created identity, scope, and policy for one Runtime tool call."""

    task_id: str
    tenant_id: str
    environment_id: str
    repository_root: str
    lease_id: str
    allowed_capabilities: frozenset[str] = field(default_factory=frozenset)
    allowed_tool_names: frozenset[str] = field(default_factory=frozenset)
    allowed_namespaces: frozenset[str] = field(default_factory=frozenset)
    max_risk_level: RiskLevel = RiskLevel.S1
    readonly_only: bool = True
    selected_tool_names: tuple[str, ...] = ()
    injected_arguments: Mapping[str, JSONValue] = field(default_factory=dict)
    call_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "task_id",
            "tenant_id",
            "environment_id",
            "repository_root",
            "lease_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.call_id is not None and not self.call_id.strip():
            raise ValueError("call_id must be non-empty when supplied")


@dataclass(frozen=True)
class ToolGatewayResult:
    """Public, provider-detail-free projection of a governed tool result."""

    status: ToolStatus
    summary: str
    data: JSONValue | None
    evidence_refs: tuple[str, ...] = ()
    reason_code: str | None = None
    retryable: bool = False


class RuntimeToolGateway:
    """Bind a model Decision to the existing governed ToolRuntime boundary."""

    def __init__(
        self,
        tool_runtime: ToolRuntime | GovernedToolRuntime,
        tool_specs: Mapping[str, ToolSpecV2],
        *,
        max_tool_schemas: int = _MAX_TOOL_SCHEMAS,
    ) -> None:
        if max_tool_schemas < 1 or max_tool_schemas > _MAX_TOOL_SCHEMAS:
            raise ValueError(f"max_tool_schemas must be between 1 and {_MAX_TOOL_SCHEMAS}")
        self._tool_runtime = tool_runtime
        self._tool_specs = dict(tool_specs)
        self._max_tool_schemas = max_tool_schemas

    def model_tool_schemas(self, context: RuntimeToolContext) -> tuple[dict[str, JSONValue], ...]:
        """Return at most three server-selected, policy-allowed tool schemas."""

        return tuple(
            {
                "name": spec.name,
                "input_schema": spec.input_schema,
                "readonly": spec.readonly,
            }
            for spec in self._selected_specs(context)
        )

    async def invoke(
        self, decision: Decision, context: RuntimeToolContext
    ) -> ToolGatewayResult:
        """Invoke a tool only after server-side selection and argument binding."""

        if decision.kind is not DecisionKind.TOOL_CALL or not decision.tool_name:
            return self._rejected("DECISION_NOT_TOOL_CALL")
        selected = {item.name for item in self._selected_specs(context)}
        if decision.tool_name not in selected:
            return self._rejected("TOOL_NOT_SELECTED")

        protected_arguments = _SERVER_CONTROLLED_ARGUMENTS | set(
            context.injected_arguments
        )
        if set(decision.arguments) & protected_arguments:
            return self._rejected("SERVER_ARGUMENT_FORBIDDEN")

        bound_arguments = dict(context.injected_arguments)
        bound_arguments.update(decision.arguments)
        call = ToolCallV2(
            call_id=context.call_id or f"call_{uuid4().hex}",
            task_id=context.task_id,
            tenant_id=context.tenant_id,
            tool_name=decision.tool_name,
            arguments=bound_arguments,
        )
        tool_context = ToolRuntimeContext(
            tenant_id=context.tenant_id,
            environment_id=context.environment_id,
            allowed_capabilities=context.allowed_capabilities,
            allowed_tool_names=frozenset(selected),
            allowed_namespaces=context.allowed_namespaces,
            max_risk_level=context.max_risk_level,
            readonly_only=context.readonly_only,
        )
        try:
            result = await self._tool_runtime.invoke(call, tool_context)
        except (TimeoutError, asyncio.TimeoutError):
            return ToolGatewayResult(
                status=ToolStatus.TIMED_OUT,
                summary="工具执行超时。",
                data=None,
                reason_code="TOOL_TIMEOUT",
                retryable=True,
            )
        except Exception:
            return ToolGatewayResult(
                status=ToolStatus.FAILED,
                summary="工具执行失败。",
                data=None,
                reason_code="TOOL_EXECUTION_FAILED",
            )
        return self._public_result(result)

    def _selected_specs(self, context: RuntimeToolContext) -> tuple[ToolSpecV2, ...]:
        names: Iterable[str]
        if context.selected_tool_names:
            names = context.selected_tool_names
        elif context.allowed_tool_names:
            names = sorted(context.allowed_tool_names)
        else:
            names = sorted(self._tool_specs)
        selected: list[ToolSpecV2] = []
        for name in names:
            spec = self._tool_specs.get(name)
            if spec is None:
                continue
            if context.allowed_tool_names and name not in context.allowed_tool_names:
                continue
            selected.append(spec)
            if len(selected) == self._max_tool_schemas:
                break
        return tuple(selected)

    @staticmethod
    def _rejected(reason_code: str) -> ToolGatewayResult:
        return ToolGatewayResult(
            status=ToolStatus.REJECTED,
            summary="工具调用被运行时策略拒绝。",
            data=None,
            reason_code=reason_code,
        )

    @staticmethod
    def _public_result(result: ToolResultV2) -> ToolGatewayResult:
        if result.status is ToolStatus.SUCCEEDED:
            return ToolGatewayResult(
                status=result.status,
                summary=result.summary,
                data=result.data,
                evidence_refs=result.evidence_refs,
            )
        reason_code = RuntimeToolGateway._public_reason_code(result)
        if result.status is ToolStatus.TIMED_OUT:
            summary = "工具执行超时。"
        elif result.status is ToolStatus.REJECTED:
            summary = "工具调用被运行时策略拒绝。"
        else:
            summary = "工具执行失败。"
        return ToolGatewayResult(
            status=result.status,
            summary=summary,
            data=None,
            evidence_refs=result.evidence_refs,
            reason_code=reason_code,
            retryable=result.retryable,
        )

    @staticmethod
    def _public_reason_code(result: ToolResultV2) -> str:
        if result.status is ToolStatus.TIMED_OUT:
            return "TOOL_TIMEOUT"
        if result.error_code in _PUBLIC_REASON_CODES:
            return result.error_code
        if result.status is ToolStatus.REJECTED:
            return "TOOL_POLICY_REJECTED"
        return "TOOL_EXECUTION_FAILED"
