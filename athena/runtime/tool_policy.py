"""Deterministic policy helpers owned by the Agent Runtime."""

from __future__ import annotations

from collections.abc import Mapping

from athena.types import JSONValue


def validate_tool_arguments(
    schema: Mapping[str, JSONValue], arguments: Mapping[str, JSONValue]
) -> str | None:
    """Validate model or Skill arguments against a Runtime tool schema."""

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
        if isinstance(expected, str) and not _matches_json_type(value, expected):
            return "TOOL_ARGUMENT_INVALID"
    return None


def _matches_json_type(value: JSONValue, expected: str) -> bool:
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


__all__ = ["validate_tool_arguments"]
