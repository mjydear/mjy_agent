"""Runtime tool catalog for backend adapters."""

from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from athena.runtime.models import Artifact, Evidence, utc_now
from athena.runtime.tools import ToolExecution
from .contracts import BackendQuery, BackendToolAdapter
from .ecommerce import ECOMMERCE_READONLY_TOOL_DEFINITIONS

class BackendReadOnlyToolCatalog:
    """Runtime-compatible catalog that turns adapter results into Evidence."""

    def __init__(self, adapter: BackendToolAdapter) -> None:
        self._adapter = adapter
        self._definitions = tuple(adapter.tool_definitions)

    @property
    def declarations(self):
        return tuple(item.as_runtime_declaration() for item in self._definitions)

    @property
    def tool_specs(self):
        return tuple(item.as_spec() for item in self._definitions)

    def has(self, tool_name: str) -> bool:
        return any(item.name == tool_name for item in self._definitions)

    def invoke(
        self,
        *,
        task_id: str,
        tick_id: str,
        repository_root: str,
        tool_name: str,
        arguments: dict[str, JSONValue],
    ) -> ToolExecution:
        del repository_root
        result = self._adapter.query(BackendQuery(tool_name, arguments))
        if not result.success:
            assert result.error_code is not None
            return ToolExecution(
                None,
                None,
                result.error_code.value,
                result.summary,
            )
        serialized = json.dumps(result.data, ensure_ascii=False, sort_keys=True)
        artifact_id = f"artifact_{uuid4().hex}"
        now = utc_now()
        artifact = Artifact(
            artifact_id=artifact_id,
            task_id=task_id,
            tick_id=tick_id,
            tool_name=tool_name,
            content=result.data,
            content_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
            created_at=now,
        )
        evidence = Evidence(
            evidence_id=f"evidence_{uuid4().hex}",
            task_id=task_id,
            artifact_id=artifact_id,
            source=f"tool:{tool_name}",
            summary=result.summary,
            created_at=now,
        )
        return ToolExecution(artifact, evidence)


__all__ = [
    "BackendReadOnlyToolCatalog",
    "ECOMMERCE_READONLY_TOOL_DEFINITIONS",
]
