"""Stable contracts shared by policy, workflow, and tool runtime code."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum

from athena.types import JSONValue


class EnvironmentMode(StrEnum):
    LIVE = "live"
    REPLAY = "replay"
    MOCK = "mock"


class DataOrigin(StrEnum):
    LIVE = "live"
    REPLAY = "replay"
    MOCK = "mock"
    DOCUMENT = "document"


class RiskLevel(StrEnum):
    S0 = "S0"
    S1 = "S1"
    S2 = "S2"
    S3 = "S3"
    S4 = "S4"
    S5 = "S5"


class ExecutionProfile(StrEnum):
    DIRECT_WORKFLOW = "direct_workflow"
    BOUNDED_POLICY_LOOP = "bounded_policy_loop"
    PLAN_EXECUTE = "plan_execute"


class ToolStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"


def _require_identifier(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")


@dataclass(frozen=True)
class ActionDecision:
    """One model-selected action after deterministic governance is applied."""

    action: str
    arguments: dict[str, JSONValue] = field(default_factory=dict)
    reason_code: str = ""
    confidence: float | None = None

    def __post_init__(self) -> None:
        _require_identifier(self.action, "action")
        _require_identifier(self.reason_code, "reason_code")
        if self.confidence is not None and (
            not math.isfinite(self.confidence) or not 0.0 <= self.confidence <= 1.0
        ):
            raise ValueError("confidence must be between 0 and 1")


@dataclass(frozen=True)
class ToolSpecV2:
    """Governed metadata for a stable tool capability."""

    name: str
    version: str
    domain: str
    input_schema: dict[str, JSONValue]
    output_schema: dict[str, JSONValue]
    required_capabilities: tuple[str, ...]
    risk_level: RiskLevel
    readonly: bool
    idempotent: bool
    timeout_seconds: float

    def __post_init__(self) -> None:
        _require_identifier(self.name, "name")
        _require_identifier(self.version, "version")
        _require_identifier(self.domain, "domain")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if self.readonly and self.risk_level not in {
            RiskLevel.S0,
            RiskLevel.S1,
            RiskLevel.S2,
        }:
            raise ValueError(
                "readonly tools cannot declare a write-operation risk level"
            )


@dataclass(frozen=True)
class ToolCallV2:
    call_id: str
    task_id: str
    tenant_id: str
    tool_name: str
    arguments: dict[str, JSONValue] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("call_id", self.call_id),
            ("task_id", self.task_id),
            ("tenant_id", self.tenant_id),
            ("tool_name", self.tool_name),
        ):
            _require_identifier(value, field_name)


@dataclass(frozen=True)
class ToolResultV2:
    status: ToolStatus
    summary: str
    data: JSONValue | None
    evidence_refs: tuple[str, ...] = ()
    error_code: str | None = None
    retryable: bool = False

    def __post_init__(self) -> None:
        _require_identifier(self.summary, "summary")
        if self.status is ToolStatus.SUCCEEDED and self.error_code is not None:
            raise ValueError("succeeded results cannot contain an error_code")
        if self.status is not ToolStatus.SUCCEEDED and not self.error_code:
            raise ValueError("unsuccessful results require an error_code")
        if any(not ref.strip() for ref in self.evidence_refs):
            raise ValueError("evidence_refs must not contain blank values")
