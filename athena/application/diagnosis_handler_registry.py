"""Route durable Kubernetes diagnosis tasks to bounded readonly handlers."""

from __future__ import annotations

import inspect
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from athena.api.repositories import PersistedTask
from athena.application.durable_worker import TaskHandler, WorkerOutcome

if TYPE_CHECKING:
    from athena.api.repositories import EvidenceRepository
    from athena.config import AthenaSettings


SUPPORTED_DIAGNOSIS_WORKFLOWS: tuple[str, ...] = (
    "crashloop",
    "pod_pending",
    "image_pull",
    "resource_pressure",
)
# Short alias for callers that describe these values as workflow types.
SUPPORTED_WORKFLOW_TYPES = SUPPORTED_DIAGNOSIS_WORKFLOWS
UNSUPPORTED_DIAGNOSIS_WORKFLOW = "UNSUPPORTED_DIAGNOSIS_WORKFLOW"
DIAGNOSIS_HANDLER_UNAVAILABLE = "DIAGNOSIS_HANDLER_UNAVAILABLE"

_MISSING = object()
_NORMALIZE_RE = re.compile(r"[^a-z0-9]+")

_WORKFLOW_ALIASES: dict[str, str] = {
    "crashloop": "crashloop",
    "crashloopbackoff": "crashloop",
    "crash_loop": "crashloop",
    "crash_loop_backoff": "crashloop",
    "pod_pending": "pod_pending",
    "podpending": "pod_pending",
    "pending": "pod_pending",
    "image_pull": "image_pull",
    "imagepull": "image_pull",
    "image_pull_failure": "image_pull",
    "imagepullfailure": "image_pull",
    "resource_pressure": "resource_pressure",
    "resourcepressure": "resource_pressure",
    "scheduling_resource_pressure": "resource_pressure",
}

# These are deliberately finite, stable alert vocabulary. Free-form objective
# text is only used as a hint; it never becomes a handler name.
_HINT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "image_pull",
        (
            "imagepullbackoff",
            "errimagepull",
            "image_pull",
            "imagepull",
        ),
    ),
    (
        "resource_pressure",
        (
            "resource_pressure",
            "resourcepressure",
            "failedscheduling",
            "unschedulable",
            "nodepressure",
            "memorypressure",
            "diskpressure",
            "pidpressure",
            "insufficient_cpu",
            "insufficient_memory",
        ),
    ),
    (
        "crashloop",
        (
            "kubepodcrashlooping",
            "crashloopbackoff",
            "crashloop",
        ),
    ),
    (
        "pod_pending",
        (
            "kubepodpending",
            "podpending",
            "pod_pending",
            "pending",
        ),
    ),
)

_HINT_KEYS: tuple[str, ...] = (
    "alert_name",
    "alertname",
    "alert_type",
    "alerttype",
    "alert",
    "labels",
    "diagnosis_hint",
    "diagnosis_workflow",
    "pod_status",
    "status",
    "reason",
    "symptom",
)


def _normalized(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return _NORMALIZE_RE.sub("_", value.strip().lower()).strip("_")


def _canonical_workflow(value: object) -> str | None:
    normalized = _normalized(value)
    if not normalized:
        return None
    return _WORKFLOW_ALIASES.get(normalized)


def _safe_requested(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:80] if text else None


def _has_explicit_value(value: object) -> bool:
    return value is not _MISSING and value is not None and bool(str(value).strip())


def _read_field(task: object, name: str) -> object:
    if isinstance(task, Mapping):
        return task.get(name, _MISSING)
    return getattr(task, name, _MISSING)


def _explicit_workflow(task: object) -> tuple[str | None, str | None, str | None]:
    """Return canonical type, requested value and source for explicit fields."""
    direct = _read_field(task, "workflow_type")
    if _has_explicit_value(direct):
        return _canonical_workflow(direct), _safe_requested(direct), "workflow_type"

    for container_name in ("scope", "state", "config_snapshot"):
        container = _read_field(task, container_name)
        if not isinstance(container, Mapping):
            continue
        for key in ("workflow_type", "diagnosis_workflow"):
            value = container.get(key, _MISSING)
            if _has_explicit_value(value):
                return (
                    _canonical_workflow(value),
                    _safe_requested(value),
                    f"{container_name}.{key}",
                )
    return None, None, None


def _hint_values(task: object) -> tuple[tuple[str, str], ...]:
    """Collect only allowlisted alert/scope fields used for fallback routing."""
    values: list[tuple[str, str]] = []

    objective = _read_field(task, "objective")
    if isinstance(objective, str) and objective.strip():
        values.append(("objective", objective))

    for direct_name in ("alert_name", "alertname", "alert_type"):
        value = _read_field(task, direct_name)
        if isinstance(value, str) and value.strip():
            values.append((f"task.{direct_name}", value))

    for container_name in ("scope", "state"):
        container = _read_field(task, container_name)
        if not isinstance(container, Mapping):
            continue
        for key in _HINT_KEYS:
            value = container.get(key, _MISSING)
            if isinstance(value, str) and value.strip():
                values.append((f"{container_name}.{key}", value))
            elif isinstance(value, Mapping):
                for nested_key in (
                    "name",
                    "alert_name",
                    "alertname",
                    "reason",
                    "type",
                    "status",
                ):
                    nested = value.get(nested_key, _MISSING)
                    if isinstance(nested, str) and nested.strip():
                        values.append((f"{container_name}.{key}.{nested_key}", nested))
    return tuple(values)


def _hint_workflow(task: object) -> tuple[str | None, str | None]:
    for source, value in _hint_values(task):
        normalized = _normalized(value)
        if not normalized:
            continue
        for workflow, markers in _HINT_MARKERS:
            for marker in markers:
                normalized_marker = _normalized(marker)
                if normalized_marker == "pending":
                    matched = normalized == marker or f"_{marker}_" in f"_{normalized}_"
                else:
                    matched = normalized_marker in normalized
                if matched:
                    return workflow, source
    return None, None


@dataclass(frozen=True)
class UnsupportedDiagnosisHandler:
    """Terminal handler used when routing cannot safely select a workflow."""

    requested_workflow: str | None
    routing_source: str
    error_code: str = UNSUPPORTED_DIAGNOSIS_WORKFLOW

    async def __call__(self, task: PersistedTask) -> WorkerOutcome:
        del task
        return WorkerOutcome(
            state={
                "error_code": self.error_code,
                "workflow_type": self.requested_workflow,
                "routing_source": self.routing_source,
            },
            phase="report",
            status="failed",
            event_type="task.failed",
            retry_delay_seconds=None,
            error_code=self.error_code,
        )


class DiagnosisHandlerRegistry:
    """Resolve a durable task to one explicitly registered readonly handler."""

    def __init__(
        self,
        handlers: Mapping[str, TaskHandler] | None = None,
        **named_handlers: TaskHandler,
    ) -> None:
        combined: dict[str, TaskHandler] = {}
        if handlers is not None:
            combined.update(handlers)
        combined.update(named_handlers)

        self._handlers: dict[str, TaskHandler] = {}
        for name, handler in combined.items():
            canonical = _canonical_workflow(name)
            if canonical is None:
                raise ValueError(f"unsupported diagnosis workflow registration: {name}")
            if canonical in self._handlers:
                raise ValueError(f"duplicate diagnosis workflow registration: {canonical}")
            if not callable(handler):
                raise TypeError(f"diagnosis handler is not callable: {canonical}")
            self._handlers[canonical] = handler

    @classmethod
    def from_settings(
        cls,
        settings: AthenaSettings,
        evidence: EvidenceRepository | None = None,
    ) -> DiagnosisHandlerRegistry:
        """Build the default four-route registry from readonly handlers."""
        from athena.application.durable_crashloop_handler import (
            DurableCrashLoopHandler,
        )

        return cls(
            {
                workflow: DurableCrashLoopHandler(
                    settings, evidence, workflow_type=workflow
                )
                for workflow in SUPPORTED_DIAGNOSIS_WORKFLOWS
            }
        )

    @property
    def supported_workflows(self) -> tuple[str, ...]:
        return tuple(
            workflow
            for workflow in SUPPORTED_DIAGNOSIS_WORKFLOWS
            if workflow in self._handlers
        )

    def resolve(self, task: PersistedTask) -> TaskHandler:
        """Resolve without side effects; unknown routes become terminal handlers."""
        canonical, requested, source = _explicit_workflow(task)
        if source is not None:
            if canonical is None:
                return UnsupportedDiagnosisHandler(
                    requested, source, UNSUPPORTED_DIAGNOSIS_WORKFLOW
                )
            return self._registered_or_unavailable(canonical, requested, source)

        canonical, hint_source = _hint_workflow(task)
        if canonical is not None:
            return self._registered_or_unavailable(canonical, canonical, hint_source or "hint")

        return UnsupportedDiagnosisHandler(
            None, "unresolved", UNSUPPORTED_DIAGNOSIS_WORKFLOW
        )

    async def __call__(self, task: PersistedTask) -> WorkerOutcome:
        """Keep the registry compatible with DurableTaskWorker's handler seam."""
        handler = self.resolve(task)
        result = handler(task)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, WorkerOutcome):
            raise TypeError("diagnosis handler must return WorkerOutcome")
        return result

    def _registered_or_unavailable(
        self, canonical: str, requested: str | None, source: str
    ) -> TaskHandler:
        handler = self._handlers.get(canonical)
        if handler is not None:
            return handler
        return UnsupportedDiagnosisHandler(
            requested or canonical, source, DIAGNOSIS_HANDLER_UNAVAILABLE
        )


__all__ = [
    "DIAGNOSIS_HANDLER_UNAVAILABLE",
    "SUPPORTED_DIAGNOSIS_WORKFLOWS",
    "SUPPORTED_WORKFLOW_TYPES",
    "UNSUPPORTED_DIAGNOSIS_WORKFLOW",
    "DiagnosisHandlerRegistry",
    "UnsupportedDiagnosisHandler",
]
