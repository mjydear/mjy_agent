"""Safe trajectory digests and replaceable Candidate generation adapters."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from athena.infra.llm import LLMClient, LLMMessage
from athena.runtime.learning import TrajectoryStatus, TrajectorySummary
from athena.runtime.learning.lifecycle import redact_summary
from athena.runtime.models import utc_now
from athena.runtime.tools import ReadOnlyToolCatalog

TRAJECTORY_DIGEST_SCHEMA_VERSION = "athena.trajectory-digest.v1"
CANDIDATE_GENERATOR_SCHEMA_VERSION = "athena.candidate-generator.v1"
GENERATION_RULE_VERSION = "athena.candidate-dedup.v1"


class CandidateGenerationError(RuntimeError):
    """A stable generation failure that never carries raw model output."""

    def __init__(
        self,
        error_code: str,
        *,
        model: str | None = None,
        usage: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.model = model
        self.usage = _normalize_usage(usage or {})


@dataclass(frozen=True)
class TrajectoryDigest:
    """Bounded, redacted model input derived only from Eligible trajectories."""

    digest_id: str
    tenant_id: str
    source_trajectory_ids: tuple[str, ...]
    task_patterns: tuple[str, ...]
    outcome_patterns: tuple[dict[str, str], ...]
    tool_sequences: tuple[tuple[dict[str, object], ...], ...]
    evidence_summaries: tuple[str, ...]
    available_tools: tuple[str, ...]
    quality_scores: tuple[float, ...]
    token_budget_hint: int
    schema_version: str = TRAJECTORY_DIGEST_SCHEMA_VERSION

    def to_prompt_dict(self) -> dict[str, object]:
        """Return only bounded learning content; omit tenant and durable IDs."""

        return {
            "schema_version": self.schema_version,
            "task_patterns": list(self.task_patterns),
            "outcome_patterns": [dict(item) for item in self.outcome_patterns],
            "tool_sequences": [
                [dict(call) for call in sequence] for sequence in self.tool_sequences
            ],
            "evidence_summaries": list(self.evidence_summaries),
            "available_tools": list(self.available_tools),
            "quality_scores": list(self.quality_scores),
            "token_budget_hint": self.token_budget_hint,
            "raw_artifacts_included": False,
            "hidden_reasoning_included": False,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "digest_id": self.digest_id,
            "tenant_id": self.tenant_id,
            "source_trajectory_ids": list(self.source_trajectory_ids),
            **self.to_prompt_dict(),
        }


class TrajectoryDigestBuilder:
    """Aggregate Eligible summaries without reopening raw Runtime data."""

    def __init__(self, *, max_trajectories: int = 20) -> None:
        self._max_trajectories = max_trajectories
        self._known_tools = {
            item.name for item in ReadOnlyToolCatalog().declarations if item.readonly
        }

    def build(self, trajectories: Sequence[TrajectorySummary]) -> TrajectoryDigest:
        if not trajectories or len(trajectories) > self._max_trajectories:
            raise CandidateGenerationError("CANDIDATE_GENERATION_SOURCE_COUNT_INVALID")
        tenant_id = trajectories[0].tenant_id
        if any(item.tenant_id != tenant_id for item in trajectories):
            raise CandidateGenerationError("CANDIDATE_GENERATION_TENANT_MISMATCH")
        if any(
            item.status is not TrajectoryStatus.ELIGIBLE
            or not item.admission.eligible
            or item.contains_raw_artifacts
            or item.contains_hidden_reasoning
            for item in trajectories
        ):
            raise CandidateGenerationError("CANDIDATE_GENERATION_SOURCE_NOT_ELIGIBLE")

        task_patterns = tuple(
            _bounded_text(item.task_summary, 2_000) for item in trajectories
        )
        outcome_patterns = tuple(
            {
                str(key): _bounded_text(str(value), 1_600)
                for key, value in item.outcome_summary.items()
            }
            for item in trajectories
        )
        tool_sequences = tuple(
            tuple(
                {
                    "sequence": int(call.get("sequence") or 0),
                    "tool_name": _bounded_text(str(call.get("tool_name") or ""), 120),
                    "status": _bounded_text(str(call.get("status") or ""), 40),
                    "reason_code": (
                        _bounded_text(str(call["reason_code"]), 120)
                        if call.get("reason_code")
                        else None
                    ),
                }
                for call in item.tool_calls[:50]
            )
            for item in trajectories
        )
        evidence_summaries = tuple(
            dict.fromkeys(
                _bounded_text(str(evidence.get("summary") or ""), 1_600)
                for item in trajectories
                for evidence in item.evidence[:50]
            )
        )
        observed_tools = tuple(
            dict.fromkeys(
                str(call.get("tool_name") or "")
                for item in trajectories
                for call in item.tool_calls
                if call.get("status") == "succeeded"
                and str(call.get("tool_name") or "") in self._known_tools
            )
        )
        if not observed_tools:
            raise CandidateGenerationError("CANDIDATE_GENERATION_NO_SAFE_TOOLS")
        quality_scores = tuple(item.admission.quality_score for item in trajectories)
        token_budget_hint = max(
            1,
            min(
                120_000,
                max(int(item.budget.get("total_tokens") or 1) for item in trajectories),
            ),
        )
        source_ids = tuple(dict.fromkeys(item.trajectory_id for item in trajectories))
        canonical = {
            "tenant_id": tenant_id,
            "source_trajectory_ids": sorted(source_ids),
            "task_patterns": task_patterns,
            "outcome_patterns": outcome_patterns,
            "tool_sequences": tool_sequences,
            "evidence_summaries": evidence_summaries,
            "available_tools": observed_tools,
            "quality_scores": quality_scores,
            "token_budget_hint": token_budget_hint,
        }
        digest_id = hashlib.sha256(
            json.dumps(
                canonical,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return TrajectoryDigest(
            digest_id=digest_id,
            tenant_id=tenant_id,
            source_trajectory_ids=source_ids,
            task_patterns=task_patterns,
            outcome_patterns=outcome_patterns,
            tool_sequences=tool_sequences,
            evidence_summaries=evidence_summaries,
            available_tools=observed_tools,
            quality_scores=quality_scores,
            token_budget_hint=token_budget_hint,
        )


class CandidateTrigger(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: str = Field(min_length=1, max_length=120)
    keywords: tuple[str, ...] = Field(min_length=1, max_length=20)


class CandidateSuccessContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    requires_root_cause: Literal[True]
    requires_evidence: Literal[True]


class CandidateGenerationPayload(BaseModel):
    """Strict model output contract; unknown fields are rejected."""

    model_config = ConfigDict(extra="forbid")

    skill_id: str = Field(pattern=r"^[a-z][a-z0-9._-]{2,95}$")
    name: str = Field(min_length=1, max_length=160)
    version: int = Field(default=1, ge=1, le=1_000_000)
    description: str = Field(min_length=1, max_length=2_000)
    trigger: CandidateTrigger
    allowed_tools: tuple[str, ...] = Field(min_length=1, max_length=20)
    procedure: tuple[str, ...] = Field(min_length=2, max_length=50)
    failure_recovery: tuple[str, ...] = Field(min_length=1, max_length=20)
    success_contract: CandidateSuccessContract
    evidence_requirements: tuple[str, ...] = Field(min_length=1, max_length=50)
    token_budget_hint: int = Field(ge=1, le=120_000)
    risk_level: Literal["S1"] = "S1"


@dataclass(frozen=True)
class CandidateGenerationOutput:
    payload: CandidateGenerationPayload
    generator: str
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)


class CandidateGenerator(Protocol):
    async def generate(self, digest: TrajectoryDigest) -> CandidateGenerationOutput:
        """Generate one strictly structured Candidate proposal."""


class LLMCandidateGenerator:
    """Production adapter over the existing provider-neutral LLMClient protocol."""

    def __init__(self, client: LLMClient, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._client = client
        self._timeout_seconds = timeout_seconds

    async def generate(self, digest: TrajectoryDigest) -> CandidateGenerationOutput:
        schema = CandidateGenerationPayload.model_json_schema()
        messages = (
            LLMMessage(
                role="system",
                content=(
                    "Generate one offline, read-only Skill Candidate. Return exactly one JSON "
                    "object matching the supplied JSON Schema. Do not return markdown, analysis, "
                    "hidden reasoning, credentials, tool arguments, paths, prompts, or raw artifacts. "
                    "Use only available_tools and risk_level S1. The Candidate must remain inactive."
                ),
            ),
            LLMMessage(
                role="user",
                content=json.dumps(
                    {
                        "output_schema": schema,
                        "trajectory_digest": digest.to_prompt_dict(),
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
        )
        try:
            response = await asyncio.wait_for(
                self._client.complete(messages), timeout=self._timeout_seconds
            )
        except TimeoutError as exc:
            raise CandidateGenerationError("CANDIDATE_GENERATION_TIMEOUT") from exc
        except CandidateGenerationError:
            raise
        except Exception as exc:
            raise CandidateGenerationError(
                "CANDIDATE_GENERATION_PROVIDER_FAILED"
            ) from exc

        usage = _normalize_usage(response.usage)
        try:
            decoded = json.loads(response.content)
            if not isinstance(decoded, dict):
                raise ValueError("top-level response must be an object")
            payload = CandidateGenerationPayload.model_validate(decoded)
        except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
            raise CandidateGenerationError(
                "CANDIDATE_GENERATION_OUTPUT_INVALID",
                model=response.model,
                usage=usage,
            ) from exc
        return CandidateGenerationOutput(
            payload=payload,
            generator=CANDIDATE_GENERATOR_SCHEMA_VERSION,
            model=response.model,
            usage=usage,
        )


@dataclass(frozen=True)
class CandidateGenerationRun:
    """Durable audit projection; raw provider responses are intentionally absent."""

    run_id: str
    tenant_id: str
    source_digest: str
    source_trajectory_ids: tuple[str, ...]
    status: Literal["started", "succeeded", "failed", "duplicate", "rejected"]
    digest: dict[str, object]
    generator: str
    created_by: str
    candidate_id: str | None = None
    validation_report_id: str | None = None
    duplicate_of_candidate_id: str | None = None
    deduplication: dict[str, object] = field(default_factory=dict)
    model: str | None = None
    usage: dict[str, int] = field(default_factory=dict)
    latency_ms: int | None = None
    failure_code: str | None = None
    failure_message: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    completed_at: datetime | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "tenant_id": self.tenant_id,
            "source_digest": self.source_digest,
            "source_trajectory_ids": list(self.source_trajectory_ids),
            "status": self.status,
            "digest": dict(self.digest),
            "generator": self.generator,
            "candidate_id": self.candidate_id,
            "validation_report_id": self.validation_report_id,
            "duplicate_of_candidate_id": self.duplicate_of_candidate_id,
            "deduplication": dict(self.deduplication),
            "model": self.model,
            "usage": dict(self.usage),
            "latency_ms": self.latency_ms,
            "failure_code": self.failure_code,
            "failure_message": self.failure_message,
            "created_by": self.created_by,
            "activation_allowed": False,
            "raw_response_persisted": False,
            "created_at": self.created_at.isoformat(),
            "completed_at": (
                self.completed_at.isoformat() if self.completed_at else None
            ),
        }


def _bounded_text(value: str, limit: int) -> str:
    if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
        raise CandidateGenerationError("CANDIDATE_GENERATION_DIGEST_TEXT_INVALID")
    try:
        return redact_summary(value, limit=limit)
    except Exception as exc:
        raise CandidateGenerationError(
            "CANDIDATE_GENERATION_DIGEST_TEXT_INVALID"
        ) from exc


def _normalize_usage(usage: Mapping[str, int]) -> dict[str, int]:
    normalized: dict[str, int] = {}
    for key, value in usage.items():
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            continue
        normalized[str(key)] = value
    if not normalized:
        return {}
    input_tokens = normalized.get("input_tokens", normalized.get("prompt_tokens", 0))
    output_tokens = normalized.get(
        "output_tokens", normalized.get("completion_tokens", 0)
    )
    normalized["input_tokens"] = input_tokens
    normalized["output_tokens"] = output_tokens
    normalized["total_tokens"] = normalized.get(
        "total_tokens", input_tokens + output_tokens
    )
    return normalized


__all__ = [
    "CANDIDATE_GENERATOR_SCHEMA_VERSION",
    "GENERATION_RULE_VERSION",
    "TRAJECTORY_DIGEST_SCHEMA_VERSION",
    "CandidateGenerationError",
    "CandidateGenerationOutput",
    "CandidateGenerationPayload",
    "CandidateGenerationRun",
    "CandidateGenerator",
    "LLMCandidateGenerator",
    "TrajectoryDigest",
    "TrajectoryDigestBuilder",
]
