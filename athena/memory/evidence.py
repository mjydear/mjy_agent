"""Evidence contracts. Content storage is intentionally introduced in Phase 1."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from athena.agent.policy.contracts import DataOrigin


@dataclass(frozen=True)
class Evidence:
    """A referenced observation, distinct from an inferred fact or hypothesis."""

    id: str
    tenant_id: str
    task_id: str
    type: str
    source: str
    data_origin: DataOrigin
    summary: str
    content_ref: str | None
    content_hash: str
    observed_at: datetime
    collected_at: datetime

    def __post_init__(self) -> None:
        for field_name, value in (
            ("id", self.id),
            ("tenant_id", self.tenant_id),
            ("task_id", self.task_id),
            ("type", self.type),
            ("source", self.source),
            ("summary", self.summary),
            ("content_hash", self.content_hash),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")
        if self.observed_at.tzinfo is None or self.collected_at.tzinfo is None:
            raise ValueError("evidence timestamps must be timezone-aware")
        if self.collected_at < self.observed_at:
            raise ValueError("collected_at must not precede observed_at")
