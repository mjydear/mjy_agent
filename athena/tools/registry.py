"""Decorator-based tool registration and invocation."""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeAlias, cast

from athena.exceptions import ErrorCode
from athena.types import JSONValue
logger = logging.getLogger(__name__)

ToolHandler: TypeAlias = Callable[..., JSONValue | Awaitable[JSONValue]]


@dataclass(frozen=True)
class Tool:
    """Registered tool metadata."""

    name: str
    description: str
    parameters: Mapping[str, str]
    required_parameters: tuple[str, ...]
    handler: ToolHandler


@dataclass(frozen=True)
class ToolCall:
    """A tool call requested by the agent."""

    name: str
    arguments: dict[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """Normalized tool result returned to the agent loop."""

    success: bool
    content: str
    error: str | None = None


class ToolRegistry:
    """Tool registry supporting decorator-based registration."""

    def __init__(self) -> None:
        """Initialize an empty tool registry."""
        self.tools: dict[str, Tool] = {}

    def register(self, func: ToolHandler) -> ToolHandler:
        """Register a tool function.

        Args:
            func: Callable to expose to the agent.

        Returns:
            The same function, preserving decorator ergonomics.
        """
        signature = inspect.signature(func)
        parameters = {
            name: str(parameter.annotation)
            for name, parameter in signature.parameters.items()
        }
        required_parameters = tuple(
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
        )
        tool = Tool(
            name=func.__name__,
            description=inspect.getdoc(func) or "",
            parameters=parameters,
            required_parameters=required_parameters,
            handler=func,
        )
        self.tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)
        return func

    def describe_tools(self) -> str:
        """Render all tools for prompt injection."""
        if not self.tools:
            return "No tools are available."
        lines: list[str] = []
        for tool in self.tools.values():
            params = ", ".join(f"{name}: {type_name}" for name, type_name in tool.parameters.items())
            lines.append(f"- {tool.name}({params}): {tool.description}")
        return "\n".join(lines)

    async def invoke(self, call: ToolCall) -> ToolResult:
        """Invoke a registered tool with robust error capture.

        Args:
            call: Tool call request.

        Returns:
            Normalized success or failure result.
        """
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult(
                success=False,
                content="",
                error=f"Tool not found: {call.name}",
            )
        missing_parameters = [
            parameter
            for parameter in tool.required_parameters
            if parameter not in call.arguments
        ]
        if missing_parameters:
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Missing required parameter(s) for tool '{call.name}': "
                    f"{', '.join(missing_parameters)}"
                ),
            )
        try:
            result = tool.handler(**call.arguments)
            if inspect.isawaitable(result):
                value = await cast(Awaitable[JSONValue], result)
            else:
                value = await asyncio.to_thread(lambda: result)
            return ToolResult(success=True, content=str(value))
        except Exception as exc:
            logger.exception("Tool execution failed: %s", call.name)
            return ToolResult(
                success=False,
                content="",
                error=f"{ErrorCode.TOOL_EXECUTION_FAILED}: {exc}",
            )
