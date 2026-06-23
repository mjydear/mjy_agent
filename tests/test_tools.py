"""Tests for tool registration and invocation."""

from __future__ import annotations

import pytest

from athena.tools import ToolCall, ToolRegistry


@pytest.mark.asyncio
async def test_tool_registry_invokes_registered_tool() -> None:
    """Registered tools should be invokable by name."""
    registry = ToolRegistry()

    @registry.register
    def greet(name: str) -> str:
        """Return a greeting."""
        return f"hello {name}"

    result = await registry.invoke(ToolCall(name="greet", arguments={"name": "Athena"}))

    assert result.success is True
    assert result.content == "hello Athena"


@pytest.mark.asyncio
async def test_tool_registry_reports_missing_tool() -> None:
    """Missing tools should return a structured failure."""
    registry = ToolRegistry()

    result = await registry.invoke(ToolCall(name="missing"))

    assert result.success is False
    assert result.error == "Tool not found: missing"


@pytest.mark.asyncio
async def test_tool_registry_validates_required_parameters() -> None:
    """Missing required tool arguments should fail before calling the function."""
    registry = ToolRegistry()

    @registry.register
    def greet(name: str) -> str:
        """Return a greeting."""
        return f"hello {name}"

    result = await registry.invoke(ToolCall(name="greet"))

    assert result.success is False
    assert result.error == "Missing required parameter(s) for tool 'greet': name"
