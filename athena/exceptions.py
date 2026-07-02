"""Athena exception hierarchy with stable error codes."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    """Stable error codes for user-facing and testable failures."""

    CONFIG_INVALID = "CONFIG_INVALID"
    LLM_CALL_FAILED = "LLM_CALL_FAILED"
    LLM_PROVIDER_UNAVAILABLE = "LLM_PROVIDER_UNAVAILABLE"
    LLM_ALL_PROVIDERS_FAILED = "LLM_ALL_PROVIDERS_FAILED"
    LLM_CIRCUIT_OPEN = "LLM_CIRCUIT_OPEN"
    PROMPT_BUILD_FAILED = "PROMPT_BUILD_FAILED"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    TOOL_EXECUTION_FAILED = "TOOL_EXECUTION_FAILED"
    AGENT_EXECUTION_FAILED = "AGENT_EXECUTION_FAILED"
    VECTOR_STORE_FAILED = "VECTOR_STORE_FAILED"


class AthenaError(Exception):
    """Base exception carrying a stable Athena error code."""

    def __init__(self, code: ErrorCode, message: str) -> None:
        """Initialize an Athena error.

        Args:
            code: Stable machine-readable error code.
            message: Human-readable error details.
        """
        super().__init__(message)
        self.code = code
        self.message = message


class ConfigError(AthenaError):
    """Raised when configuration cannot be loaded or validated."""


class LLMError(AthenaError):
    """Raised when the LLM gateway fails."""


class PromptError(AthenaError):
    """Raised when prompt assembly fails."""


class ToolError(AthenaError):
    """Raised when tool lookup or execution fails."""


class AgentError(AthenaError):
    """Raised when the agent execution loop fails."""


class VectorStoreError(AthenaError):
    """Raised when vector storage operations fail."""
