"""Runtime gateway for governed tool invocations.

The model can choose a tool from the server-selected directory and provide
business arguments.  Identity, repository scope, permissions, call IDs, and
any injected arguments remain server-owned.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol
from uuid import uuid4

from athena.agent.policy.contracts import (
    RiskLevel,
    ToolCallV2,
    ToolResultV2,
    ToolSpecV2,
    ToolStatus,
)
from athena.types import JSONValue

from .models import Decision, DecisionKind
from .tool_policy import validate_tool_arguments
from .tools import ToolExecution
from .models import Artifact, Evidence, utc_now

_MAX_TOOL_SCHEMAS = 3
SERVER_CONTROLLED_ARGUMENTS = frozenset(
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
        "DATA_ORIGIN_FORBIDDEN",
        "SCOPE_PERMISSION_DENIED",
        "SCOPE_DENIED",
        "RESOURCE_SCOPE_DENIED",
        "PATH_OUT_OF_SCOPE",
        "REPOSITORY_FILE_NOT_FOUND",
        "BACKEND_RECORD_NOT_FOUND",
        "BACKEND_QUERY_FAILED",
        "BACKEND_UNAVAILABLE",
        "TENANT_SCOPE_VIOLATION",
        "TOOL_TIMEOUT",
        "UNKNOWN_TOOL",
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


class GovernedToolInvoker(Protocol):
    async def invoke(
        self, call: ToolCallV2, context: RuntimeToolContext
    ) -> ToolResultV2: ...


class RuntimeToolCatalog(Protocol):
    """The small catalog seam required by the Runtime gateway."""

    @property
    def declarations(self) -> tuple[Any, ...]: ...

    def has(self, tool_name: str) -> bool: ...

    def invoke(
        self,
        *,
        task_id: str,
        tick_id: str,
        repository_root: str,
        tool_name: str,
        arguments: dict[str, JSONValue],
    ) -> ToolExecution: ...


class CatalogToolInvoker:
    """Adapt a read-only catalog behind the governed async seam.

    The catalog remains an adapter; RuntimeToolGateway owns selection, schema
    checks, server binding, timeout and public error projection.
    """

    def __init__(self, catalog: RuntimeToolCatalog) -> None:
        self._catalog = catalog

    async def invoke(
        self, call: ToolCallV2, context: RuntimeToolContext
    ) -> ToolResultV2:
        arguments = dict(call.arguments)
        repository_root = str(arguments.pop("repository_root", ""))
        execution = await asyncio.to_thread(
            self._catalog.invoke,
            task_id=call.task_id,
            tick_id=call.call_id,
            repository_root=repository_root,
            tool_name=call.tool_name,
            arguments=arguments,
        )
        if not execution.succeeded:
            return ToolResultV2(
                status=ToolStatus.FAILED,
                summary=execution.error_message or "tool execution failed",
                data=None,
                error_code=execution.error_code or "TOOL_EXECUTION_FAILED",
            )
        if execution.artifact is None or execution.evidence is None:
            return ToolResultV2(
                status=ToolStatus.FAILED,
                summary="tool returned no evidence",
                data=None,
                error_code="TOOL_EVIDENCE_MISSING",
            )
        return ToolResultV2(
            status=ToolStatus.SUCCEEDED,
            summary=execution.evidence.summary,
            data=execution.artifact.content,
            evidence_refs=(execution.evidence.evidence_id,),
        )


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
    allowed_resource_scopes: frozenset[str] = field(default_factory=frozenset)
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
    """Bind a model Decision to the governed Runtime invocation seam."""

    def __init__(
        self,
        tool_invoker: GovernedToolInvoker,
        tool_specs: Mapping[str, ToolSpecV2],
        *,
        max_tool_schemas: int = _MAX_TOOL_SCHEMAS,
    ) -> None:
        if max_tool_schemas < 1 or max_tool_schemas > _MAX_TOOL_SCHEMAS:
            raise ValueError(
                f"max_tool_schemas must be between 1 and {_MAX_TOOL_SCHEMAS}"
            )
        self._tool_invoker = tool_invoker
        self._tool_specs = dict(tool_specs)
        self._max_tool_schemas = max_tool_schemas

    def model_tool_schemas(
        self, context: RuntimeToolContext
    ) -> tuple[dict[str, JSONValue], ...]:
        """Return at most three server-selected, policy-allowed tool schemas."""

        return tuple(
            {
                "name": spec.name,
                "input_schema": spec.input_schema,
                "readonly": spec.readonly,
            }
            for spec in self._selected_specs(context)[: self._max_tool_schemas]
        )

    @classmethod
    def from_catalog(cls, catalog: RuntimeToolCatalog) -> "RuntimeToolGateway":
        """Build the gateway for either the repository or backend catalog."""

        declarations = tuple(catalog.declarations)
        specs = {
            item.name: item.as_spec()
            for item in declarations
            if hasattr(item, "as_spec")
        }
        backend_specs = getattr(catalog, "tool_specs", ())
        specs.update({item.name: item for item in backend_specs})
        if not specs:
            raise ValueError("catalog must expose at least one ToolSpecV2")
        return cls(CatalogToolInvoker(catalog), specs)

    def invoke_sync(
        self, decision: Decision, context: RuntimeToolContext
    ) -> ToolGatewayResult:
        """Invoke the async gateway from the synchronous ReAct Runtime.

        FastAPI may call the synchronous Runtime while an event loop is already
        running. In that case the coroutine runs in a short-lived helper thread
        instead of nesting ``asyncio.run`` in the active loop.
        """

        coroutine = self.invoke(decision, context)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        result: list[ToolGatewayResult] = []
        failure: list[BaseException] = []

        def run() -> None:
            try:
                result.append(asyncio.run(coroutine))
            except BaseException as exc:  # pragma: no cover - defensive bridge
                failure.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        thread.join()
        if failure:
            raise failure[0]
        return result[0]

    async def invoke(
        self, decision: Decision, context: RuntimeToolContext
    ) -> ToolGatewayResult:
        """Invoke a tool only after server-side selection and argument binding."""

        if decision.kind is not DecisionKind.TOOL_CALL or not decision.tool_name:
            return self._rejected("DECISION_NOT_TOOL_CALL")
        selected = {item.name for item in self._selected_specs(context)}
        if decision.tool_name not in selected:
            return self._rejected("TOOL_NOT_SELECTED")

        spec = self._tool_specs[decision.tool_name]

        protected_arguments = SERVER_CONTROLLED_ARGUMENTS | set(
            context.injected_arguments
        )
        if set(decision.arguments) & protected_arguments:
            if "tenant_id" in decision.arguments:
                return self._rejected("TENANT_SCOPE_VIOLATION")
            return self._rejected("SERVER_ARGUMENT_FORBIDDEN")

        bound_arguments = dict(context.injected_arguments)
        bound_arguments.update(decision.arguments)
        schema_arguments = dict(decision.arguments)
        schema_error = validate_tool_arguments(spec.input_schema, schema_arguments)
        if schema_error is not None:
            return self._rejected(schema_error)
        call = ToolCallV2(
            call_id=context.call_id or f"call_{uuid4().hex}",
            task_id=context.task_id,
            tenant_id=context.tenant_id,
            tool_name=decision.tool_name,
            arguments=bound_arguments,
        )
        try:
            result = await asyncio.wait_for(
                self._tool_invoker.invoke(call, context),
                timeout=spec.timeout_seconds,
            )
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

    @staticmethod
    def materialize(
        result: ToolGatewayResult,
        *,
        task_id: str,
        tick_id: str,
        tool_name: str,
    ) -> ToolExecution:
        """Project one public gateway result into the Runtime evidence model."""

        if result.status is not ToolStatus.SUCCEEDED:
            return ToolExecution(
                None,
                None,
                result.reason_code or "TOOL_EXECUTION_FAILED",
                result.summary,
            )
        content: dict[str, Any]
        if isinstance(result.data, dict):
            content = dict(result.data)
        else:
            content = {"result": result.data}
        serialized = json.dumps(content, ensure_ascii=False, sort_keys=True)
        now = utc_now()
        artifact = Artifact(
            artifact_id=f"artifact_{uuid4().hex}",
            task_id=task_id,
            tick_id=tick_id,
            tool_name=tool_name,
            content=content,
            content_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            created_at=now,
        )
        evidence = Evidence(
            evidence_id=(
                result.evidence_refs[0]
                if result.evidence_refs
                else f"evidence_{uuid4().hex}"
            ),
            task_id=task_id,
            artifact_id=artifact.artifact_id,
            source=f"tool:{tool_name}",
            summary=result.summary,
            created_at=now,
        )
        return ToolExecution(artifact, evidence)

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
