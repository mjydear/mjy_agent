"""Tests for permissions, audit and protocol adapters."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from athena.tools import (
    AuditLogger,
    PermissionManager,
    ToolAuditEvent,
    ToolPermission,
    ToolRegistry,
)
from athena.tools.protocol import MCPToolAdapter, OpenAIToolAdapter
from athena.types import JSONValue


def test_permission_manager_requires_confirmation() -> None:
    manager = PermissionManager(
        (
            ToolPermission(
                tool_name="delete", risk_level="high", requires_confirmation=True
            ),
        )
    )

    with pytest.raises(PermissionError):
        manager.assert_allowed("delete")

    manager.assert_allowed("delete", confirmed=True)


def test_audit_logger_filters_by_tool() -> None:
    logger = AuditLogger()
    logger.record(ToolAuditEvent(tool_name="git_status", action="run", success=True))

    assert len(logger.by_tool("git_status")) == 1


def test_protocol_adapters_parse_tool_calls() -> None:
    registry = ToolRegistry()

    @registry.register
    def echo(text: str) -> str:
        """Echo text."""
        return text

    assert MCPToolAdapter().export_tools(registry)[0]["name"] == "echo"
    assert (
        MCPToolAdapter().parse_call({"name": "echo", "arguments": {"text": "hi"}}).name
        == "echo"
    )
    payload: dict[str, JSONValue] = {
        "function": {"name": "echo", "arguments": json.dumps({"text": "hi"})}
    }
    assert OpenAIToolAdapter().parse_call(payload).arguments["text"] == "hi"


def test_permission_manager_blocks_path_escape(tmp_path: Path) -> None:
    manager = PermissionManager(
        (ToolPermission(tool_name="read", allowed_directories=(tmp_path,)),)
    )

    with pytest.raises(PermissionError):
        manager.assert_allowed("read", target_path=Path("C:/Windows/system32"))
