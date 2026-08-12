"""Focused tests for the governed Runtime ToolRuntime adapter."""

from __future__ import annotations

import asyncio

from athena.agent.policy.contracts import RiskLevel, ToolResultV2, ToolSpecV2, ToolStatus
from athena.runtime.models import Decision, DecisionKind
from athena.runtime.tool_gateway import RuntimeToolContext, RuntimeToolGateway


class _RecordingToolRuntime:
    def __init__(self, result: ToolResultV2) -> None:
        self.result = result
        self.calls = []
        self.contexts = []

    async def invoke(self, call, context):
        self.calls.append(call)
        self.contexts.append(context)
        return self.result


def _spec(name: str) -> ToolSpecV2:
    return ToolSpecV2(
        name=name,
        version="1.0.0",
        domain="repository",
        input_schema={"type": "object", "properties": {"query": {"type": "string"}}},
        output_schema={"type": "object"},
        required_capabilities=("repository.read",),
        risk_level=RiskLevel.S1,
        readonly=True,
        idempotent=True,
        timeout_seconds=1.0,
    )


def _context(**overrides: object) -> RuntimeToolContext:
    values: dict[str, object] = {
        "task_id": "task-server",
        "tenant_id": "tenant-server",
        "environment_id": "env-server",
        "repository_root": "D:/safe-repository",
        "lease_id": "lease-server",
        "allowed_capabilities": frozenset({"repository.read"}),
        "allowed_tool_names": frozenset({"search", "outline", "read", "test"}),
        "selected_tool_names": ("search", "outline", "read", "test"),
        "injected_arguments": {"repository_root": "D:/safe-repository"},
        "call_id": "call-server",
    }
    values.update(overrides)
    return RuntimeToolContext(**values)  # type: ignore[arg-type]


def _decision(arguments: dict[str, object] | None = None) -> Decision:
    return Decision(
        kind=DecisionKind.TOOL_CALL,
        reason_code="SEARCH_SOURCE",
        tool_name="search",
        arguments=arguments or {"query": "discounted_price"},
    )


def test_gateway_limits_directory_to_three_server_selected_schemas() -> None:
    runtime = _RecordingToolRuntime(
        ToolResultV2(status=ToolStatus.SUCCEEDED, summary="ok", data={})
    )
    gateway = RuntimeToolGateway(
        runtime,
        {name: _spec(name) for name in ("search", "outline", "read", "test")},
    )

    schemas = gateway.model_tool_schemas(_context())

    assert [item["name"] for item in schemas] == ["search", "outline", "read"]
    assert len(schemas) == 3


def test_gateway_binds_server_values_and_rejects_model_override() -> None:
    runtime = _RecordingToolRuntime(
        ToolResultV2(status=ToolStatus.SUCCEEDED, summary="ok", data={"matches": []})
    )
    gateway = RuntimeToolGateway(runtime, {"search": _spec("search")})

    success = asyncio.run(gateway.invoke(_decision(), _context(selected_tool_names=("search",))))
    rejected = asyncio.run(
        gateway.invoke(
            _decision({"query": "x", "repository_root": "C:/attacker"}),
            _context(selected_tool_names=("search",)),
        )
    )

    assert success.status is ToolStatus.SUCCEEDED
    assert runtime.calls[0].task_id == "task-server"
    assert runtime.calls[0].tenant_id == "tenant-server"
    assert runtime.calls[0].call_id == "call-server"
    assert runtime.calls[0].arguments["repository_root"] == "D:/safe-repository"
    assert rejected.status is ToolStatus.REJECTED
    assert rejected.reason_code == "SERVER_ARGUMENT_FORBIDDEN"
    assert len(runtime.calls) == 1


def test_gateway_rejects_tools_outside_server_directory_before_dispatch() -> None:
    runtime = _RecordingToolRuntime(
        ToolResultV2(status=ToolStatus.SUCCEEDED, summary="ok", data={})
    )
    gateway = RuntimeToolGateway(runtime, {"search": _spec("search")})
    decision = Decision(
        kind=DecisionKind.TOOL_CALL,
        reason_code="READ_SOURCE",
        tool_name="read",
        arguments={},
    )

    result = asyncio.run(gateway.invoke(decision, _context(selected_tool_names=("search",))))

    assert result.status is ToolStatus.REJECTED
    assert result.reason_code == "TOOL_NOT_SELECTED"
    assert runtime.calls == []


def test_gateway_converts_provider_errors_to_public_reason_codes() -> None:
    runtime = _RecordingToolRuntime(
        ToolResultV2(
            status=ToolStatus.FAILED,
            summary="provider host token=secret-value failed",
            data=None,
            error_code="INTERNAL_PROVIDER_STACK",
        )
    )
    gateway = RuntimeToolGateway(runtime, {"search": _spec("search")})

    result = asyncio.run(gateway.invoke(_decision(), _context(selected_tool_names=("search",))))

    assert result.status is ToolStatus.FAILED
    assert result.reason_code == "TOOL_EXECUTION_FAILED"
    assert result.summary == "工具执行失败。"
    assert "secret-value" not in result.summary


def test_gateway_preserves_allowlisted_path_rejection_code() -> None:
    runtime = _RecordingToolRuntime(
        ToolResultV2(
            status=ToolStatus.REJECTED,
            summary="absolute path D:/secret is forbidden",
            data=None,
            error_code="PATH_OUT_OF_SCOPE",
        )
    )
    gateway = RuntimeToolGateway(runtime, {"search": _spec("search")})

    result = asyncio.run(gateway.invoke(_decision(), _context(selected_tool_names=("search",))))

    assert result.status is ToolStatus.REJECTED
    assert result.reason_code == "PATH_OUT_OF_SCOPE"
    assert result.summary == "工具调用被运行时策略拒绝。"
