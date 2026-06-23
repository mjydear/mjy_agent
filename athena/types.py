"""Shared JSON-compatible type aliases."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

JSONScalar = str | int | float | bool | None
JSONValue = JSONScalar | Mapping[str, "JSONValue"] | Sequence["JSONValue"]
JSONObject = Mapping[str, JSONValue]
