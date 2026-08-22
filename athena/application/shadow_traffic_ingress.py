"""Controlled ingress boundary for production-shaped Shadow traffic.

The ingress event contains references to a completed Runtime task only.  Raw
goals, tool results, artifacts, and candidate contents are deliberately not an
accepted input path; the application service resolves the task snapshot from a
trusted Runtime store and performs the existing redacted capture.
"""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from athena.api.repositories.shadow_traffic_repository import (
    ShadowTrafficObservation,
)
from athena.runtime import TaskStatus

SHADOW_TRAFFIC_INGRESS_SCHEMA_VERSION = "athena.shadow-traffic-ingress.v1"
SHADOW_TRAFFIC_INGRESS_EVENT_TYPE = "runtime.trace.completed"
DEFAULT_MAX_INGRESS_BYTES = 16_384

_ALLOWED_FIELDS = frozenset(
    {
        "event_type",
        "schema_version",
        "source",
        "tenant_id",
        "task_id",
        "trace_id",
        "candidate_id",
        "task_status",
        "traceparent",
    }
)
_TERMINAL_STATUSES = frozenset(status.value for status in TaskStatus if status.terminal)


class ShadowTrafficIngressError(ValueError):
    """Stable, non-sensitive error raised at the traffic ingress boundary."""

    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


@dataclass(frozen=True)
class ShadowTrafficIngressEvent:
    """A bounded reference to one completed Runtime trace."""

    source: str
    tenant_id: str
    task_id: str
    trace_id: str
    task_status: str
    candidate_id: str | None = None
    traceparent: str | None = None
    event_type: str = SHADOW_TRAFFIC_INGRESS_EVENT_TYPE
    schema_version: str = SHADOW_TRAFFIC_INGRESS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_text(self.source, "SHADOW_INGRESS_SOURCE_INVALID", 96)
        _require_text(self.tenant_id, "SHADOW_INGRESS_TENANT_INVALID", 120)
        _require_text(self.task_id, "SHADOW_INGRESS_TASK_INVALID", 96)
        _require_text(self.trace_id, "SHADOW_INGRESS_TRACE_INVALID", 128)
        if self.event_type != SHADOW_TRAFFIC_INGRESS_EVENT_TYPE:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_EVENT_TYPE_UNSUPPORTED")
        if self.schema_version != SHADOW_TRAFFIC_INGRESS_SCHEMA_VERSION:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_SCHEMA_UNSUPPORTED")
        if self.task_status not in _TERMINAL_STATUSES:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_TASK_NOT_TERMINAL")
        if self.candidate_id is not None:
            _require_text(self.candidate_id, "SHADOW_INGRESS_CANDIDATE_INVALID", 96)
        if self.traceparent is not None:
            _require_text(self.traceparent, "SHADOW_INGRESS_TRACEPARENT_INVALID", 256)

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object],
        *,
        max_payload_bytes: int = DEFAULT_MAX_INGRESS_BYTES,
    ) -> "ShadowTrafficIngressEvent":
        if not isinstance(payload, Mapping):
            raise ShadowTrafficIngressError("SHADOW_INGRESS_PAYLOAD_INVALID")
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        try:
            serialized = json.dumps(
                payload,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_PAYLOAD_INVALID") from exc
        if len(serialized) > max_payload_bytes:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_PAYLOAD_TOO_LARGE")
        if not all(isinstance(key, str) for key in payload):
            raise ShadowTrafficIngressError("SHADOW_INGRESS_PAYLOAD_INVALID")
        if set(payload) - _ALLOWED_FIELDS:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_UNTRUSTED_FIELD")

        return cls(
            source=_text_field(payload, "source", 96),
            tenant_id=_text_field(payload, "tenant_id", 120),
            task_id=_text_field(payload, "task_id", 96),
            trace_id=_text_field(payload, "trace_id", 128),
            task_status=_text_field(payload, "task_status", 32),
            candidate_id=_optional_text_field(payload, "candidate_id", 96),
            traceparent=_optional_text_field(payload, "traceparent", 256),
            event_type=_text_field(payload, "event_type", 96),
            schema_version=_text_field(payload, "schema_version", 64),
        )

    def to_dict(self) -> dict[str, object]:
        value: dict[str, object] = {
            "event_type": self.event_type,
            "schema_version": self.schema_version,
            "source": self.source,
            "tenant_id": self.tenant_id,
            "task_id": self.task_id,
            "trace_id": self.trace_id,
            "task_status": self.task_status,
        }
        if self.candidate_id is not None:
            value["candidate_id"] = self.candidate_id
        if self.traceparent is not None:
            value["traceparent"] = self.traceparent
        return value


class ShadowTrafficCapturePort(Protocol):
    async def capture_ingress_event(
        self, event: ShadowTrafficIngressEvent
    ) -> ShadowTrafficObservation:
        """Capture the event after resolving it against trusted Runtime state."""


class ShadowTrafficIngressAdapter:
    """Bind untrusted event input to an explicit tenant and Candidate policy."""

    def __init__(
        self,
        service: ShadowTrafficCapturePort,
        *,
        candidate_id: str,
        source: str,
        allowed_tenants: Collection[str],
        max_payload_bytes: int = DEFAULT_MAX_INGRESS_BYTES,
    ) -> None:
        _require_text(candidate_id, "SHADOW_INGRESS_CANDIDATE_INVALID", 96)
        _require_text(source, "SHADOW_INGRESS_SOURCE_INVALID", 96)
        if not allowed_tenants:
            raise ValueError("allowed_tenants must contain at least one tenant")
        if max_payload_bytes <= 0:
            raise ValueError("max_payload_bytes must be positive")
        self._service = service
        self._candidate_id = candidate_id
        self._source = source
        self._allowed_tenants = frozenset(allowed_tenants)
        self._max_payload_bytes = max_payload_bytes

    async def ingest(self, payload: Mapping[str, object]) -> ShadowTrafficObservation:
        event = ShadowTrafficIngressEvent.from_payload(
            payload,
            max_payload_bytes=self._max_payload_bytes,
        )
        if event.source != self._source:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_SOURCE_FORBIDDEN")
        if event.tenant_id not in self._allowed_tenants:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_TENANT_FORBIDDEN")
        if event.candidate_id not in {None, self._candidate_id}:
            raise ShadowTrafficIngressError("SHADOW_INGRESS_CANDIDATE_MISMATCH")
        return await self._service.capture_ingress_event(
            replace(event, candidate_id=self._candidate_id)
        )


def _text_field(payload: Mapping[str, object], name: str, max_length: int) -> str:
    value = payload.get(name)
    if not isinstance(value, str):
        raise ShadowTrafficIngressError("SHADOW_INGRESS_PAYLOAD_INVALID")
    _require_text(value, "SHADOW_INGRESS_PAYLOAD_INVALID", max_length)
    return value


def _optional_text_field(
    payload: Mapping[str, object], name: str, max_length: int
) -> str | None:
    if name not in payload or payload[name] is None:
        return None
    return _text_field(payload, name, max_length)


def _require_text(value: Any, error_code: str, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ShadowTrafficIngressError(error_code)


__all__ = [
    "DEFAULT_MAX_INGRESS_BYTES",
    "SHADOW_TRAFFIC_INGRESS_EVENT_TYPE",
    "SHADOW_TRAFFIC_INGRESS_SCHEMA_VERSION",
    "ShadowTrafficCapturePort",
    "ShadowTrafficIngressAdapter",
    "ShadowTrafficIngressError",
    "ShadowTrafficIngressEvent",
]
