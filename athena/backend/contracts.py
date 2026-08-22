"""Small contracts shared by backend scenario implementations and tool bridges."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from athena.agent.policy.contracts import RiskLevel, ToolSpecV2
from athena.types import JSONValue


class BackendErrorCode(StrEnum):
    """Stable, provider-independent failure codes for backend read tools."""

    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_ARGUMENT_REQUIRED = "TOOL_ARGUMENT_REQUIRED"
    TOOL_ARGUMENT_UNKNOWN = "TOOL_ARGUMENT_UNKNOWN"
    TOOL_ARGUMENT_INVALID = "TOOL_ARGUMENT_INVALID"
    BACKEND_RECORD_NOT_FOUND = "BACKEND_RECORD_NOT_FOUND"
    BACKEND_QUERY_FAILED = "BACKEND_QUERY_FAILED"


@dataclass(frozen=True)
class BackendQuery:
    """One validated-at-the-seam request from a tool bridge to a scenario."""

    tool_name: str
    arguments: Mapping[str, JSONValue]

    def __post_init__(self) -> None:
        if not isinstance(self.tool_name, str) or not self.tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")


@dataclass(frozen=True)
class BackendQueryResult:
    """A safe result that never exposes the adapter's raw provider exception."""

    success: bool
    summary: str
    data: JSONValue | None = None
    error_code: BackendErrorCode | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        if not self.summary.strip():
            raise ValueError("summary must be a non-empty string")
        if self.success and self.error_code is not None:
            raise ValueError("successful results cannot contain an error_code")
        if not self.success and self.error_code is None:
            raise ValueError("failed results require an error_code")


@dataclass(frozen=True)
class BackendToolDefinition:
    """Tool metadata used to build Runtime declarations and policy specs."""

    name: str
    description: str
    input_schema: dict[str, JSONValue]
    capability: str = "ecommerce.read"
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("tool name must be a non-empty string")
        if not self.description.strip():
            raise ValueError("tool description must be a non-empty string")
        if not self.capability.strip():
            raise ValueError("capability must be a non-empty string")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")

    @property
    def readonly(self) -> bool:
        return True

    def as_spec(self) -> ToolSpecV2:
        """Return the governed Tool V2 metadata for this read-only capability."""
        return ToolSpecV2(
            name=self.name,
            version="1.0.0",
            domain="ecommerce",
            input_schema=self.input_schema,
            output_schema={"type": "object"},
            required_capabilities=(self.capability,),
            risk_level=RiskLevel.S1,
            readonly=True,
            idempotent=True,
            timeout_seconds=self.timeout_seconds,
        )

    def as_runtime_declaration(self):
        """Return the declaration consumed by the Runtime catalog."""
        from athena.runtime.tools import ToolDeclaration

        return ToolDeclaration(
            name=self.name,
            description=self.description,
            input_schema=self.input_schema,
            readonly=True,
        )


class BackendScenario(Protocol):
    """Deep domain seam: dispatch one named, read-only backend query."""

    def query(self, request: BackendQuery) -> BackendQueryResult: ...


class BackendToolAdapter(BackendScenario, Protocol):
    """A scenario plus the stable tool metadata needed by Agent Runtime."""

    @property
    def tool_definitions(self) -> tuple[BackendToolDefinition, ...]: ...
