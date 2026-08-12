"""Basic built-in tools for the MVP."""

from __future__ import annotations

from datetime import UTC, datetime

from athena.tools import ToolRegistry


def register_basic_tools(registry: ToolRegistry) -> None:
    """Register safe, deterministic MVP tools."""

    @registry.register
    def echo(text: str) -> str:
        """Return the provided text unchanged."""
        return text

    @registry.register
    def current_utc_time() -> str:
        """Return the current UTC time in ISO-8601 format."""
        return datetime.now(UTC).isoformat()
