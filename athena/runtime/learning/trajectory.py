"""Deterministic, redacted summaries and admission gates for Runtime trajectories.

Raw Artifacts, model prompts, tool arguments, repository roots, and hidden
reasoning are deliberately excluded.  The output is safe to persist as a
learning fact and is not an executable Skill.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from athena.runtime.models import DecisionKind, RuntimeSnapshot, TaskStatus, utc_now

from .lifecycle import redact_summary

TRAJECTORY_SCHEMA_VERSION = "runtime.learning.trajectory.v1"


class TrajectoryStatus(StrEnum):
    OBSERVED = "observed"
    ELIGIBLE = "eligible"
    REJECTED = "rejected"


class TrajectoryRejectionReason(StrEnum):
    TASK_NOT_SUCCEEDED = "TASK_NOT_SUCCEEDED"
    EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
    SECURITY_VIOLATION = "SECURITY_VIOLATION"
    TOOL_OVERREACH = "TOOL_OVERREACH"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"


_SECURITY_REASON_CODES = frozenset(
    {
        "ENV_DATA_ORIGIN_FORBIDDEN",
        "PATH_OUT_OF_SCOPE",
        "PROMPT_INJECTION_DETECTED",
        "SECRET_EXPOSURE_DETECTED",
        "WRITE_OPERATION_FORBIDDEN",
    }
)
_TOOL_OVERREACH_REASON_CODES = frozenset(
    {
        "CAPABILITY_FORBIDDEN",
        "ENV_PERMISSION_DENIED",
        "ENV_SCOPE_DENIED",
        "OPS_NAMESPACE_FORBIDDEN",
        "RISK_LEVEL_FORBIDDEN",
        "SERVER_ARGUMENT_FORBIDDEN",
        "TOOL_NOT_ALLOWED",
        "TOOL_NOT_FOUND",
        "TOOL_NOT_SELECTED",
        "TOOL_POLICY_REJECTED",
        "UNKNOWN_TOOL",
        "WRITE_OPERATION_FORBIDDEN",
    }
)
_WINDOWS_PATH = re.compile(r"(?i)(?:[a-z]:\\|\\\\)[^\s,;]+")
_UNIX_PATH = re.compile(r"(?<![\w:])/(?:[^/\s]+/)*[^\s,;]+")
_EMAIL = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


@dataclass(frozen=True)
class TrajectoryAdmission:
    """Explainable result of all mandatory admission checks."""

    eligible: bool
    rejection_reasons: tuple[str, ...]
    quality_score: float
    quality_factors: dict[str, float]
    quality_explanations: tuple[str, ...]
    checks: dict[str, bool]


@dataclass(frozen=True)
class TrajectorySummary:
    """Bounded learning fact derived from a Runtime snapshot."""

    trajectory_id: str
    tenant_id: str
    source_task_id: str
    schema_version: str
    status: TrajectoryStatus
    task_summary: str
    outcome_summary: dict[str, str]
    tool_calls: tuple[dict[str, object], ...]
    evidence: tuple[dict[str, str], ...]
    usage: dict[str, object]
    budget: dict[str, object]
    admission: TrajectoryAdmission
    redaction_count: int
    created_at: datetime
    contains_raw_artifacts: bool = False
    contains_hidden_reasoning: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "trajectory_id": self.trajectory_id,
            "tenant_id": self.tenant_id,
            "source_task_id": self.source_task_id,
            "schema_version": self.schema_version,
            "status": self.status.value,
            "task_summary": self.task_summary,
            "outcome_summary": dict(self.outcome_summary),
            "tool_calls": [dict(item) for item in self.tool_calls],
            "evidence": [dict(item) for item in self.evidence],
            "usage": dict(self.usage),
            "budget": dict(self.budget),
            "admission": {
                "eligible": self.admission.eligible,
                "rejection_reasons": list(self.admission.rejection_reasons),
                "quality_score": self.admission.quality_score,
                "quality_factors": dict(self.admission.quality_factors),
                "quality_explanations": list(
                    self.admission.quality_explanations
                ),
                "checks": dict(self.admission.checks),
            },
            "redaction_count": self.redaction_count,
            "contains_raw_artifacts": self.contains_raw_artifacts,
            "contains_hidden_reasoning": self.contains_hidden_reasoning,
            "created_at": self.created_at.isoformat(),
        }


class TrajectorySummaryBuilder:
    """Build and score a trajectory using only observable Runtime facts."""

    def build(self, snapshot: RuntimeSnapshot, *, tenant_id: str) -> TrajectorySummary:
        if not isinstance(tenant_id, str) or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        task = snapshot.task
        evidence_by_id = {item.evidence_id: item for item in snapshot.evidence}
        final_ids = tuple(
            dict.fromkeys(task.final_report.evidence_ids if task.final_report else ())
        )
        tool_calls = self._tool_calls(snapshot)

        task_summary, task_redactions = _redact(task.goal, limit=2_000)
        redaction_count = task_redactions
        outcome_summary: dict[str, str] = {}
        if task.final_report is not None:
            root_cause, count = _redact(task.final_report.root_cause)
            redaction_count += count
            recommendation, count = _redact(
                task.final_report.repair_recommendation
            )
            redaction_count += count
            outcome_summary = {
                "root_cause": root_cause,
                "repair_recommendation": recommendation,
            }

        evidence: list[dict[str, str]] = []
        for evidence_id in final_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                continue
            source, count = _redact(item.source, limit=160)
            redaction_count += count
            summary, count = _redact(item.summary)
            redaction_count += count
            evidence.append(
                {
                    "evidence_id": evidence_id,
                    "source": source,
                    "summary": summary,
                }
            )

        actual_input_tokens = sum(item.actual_input_tokens for item in snapshot.usage)
        actual_output_tokens = sum(item.actual_output_tokens for item in snapshot.usage)
        actual_tokens = actual_input_tokens + actual_output_tokens
        usage: dict[str, object] = {
            "input_tokens": actual_input_tokens,
            "output_tokens": actual_output_tokens,
            "total_tokens": actual_tokens,
            "tick_count": len(snapshot.ticks),
            "tool_call_count": len(tool_calls),
            "successful_tool_call_count": sum(
                item["status"] == "succeeded" for item in tool_calls
            ),
        }
        budget: dict[str, object] = {
            "total_tokens": task.budget.total_tokens,
            "consumed_tokens": task.budget.consumed_tokens,
            "max_ticks": task.budget.max_ticks,
            "within_budget": (
                task.status is not TaskStatus.BUDGET_EXHAUSTED
                and task.budget.consumed_tokens <= task.budget.total_tokens
                and actual_tokens <= task.budget.total_tokens
                and len(snapshot.ticks) <= task.budget.max_ticks
            ),
        }

        admission = self._admit(
            snapshot,
            final_ids=final_ids,
            evidence_by_id=evidence_by_id,
            tool_calls=tool_calls,
            budget_within=bool(budget["within_budget"]),
        )
        status = (
            TrajectoryStatus.ELIGIBLE
            if admission.eligible
            else TrajectoryStatus.REJECTED
        )
        trajectory_id = _trajectory_id(tenant_id.strip(), task.task_id)
        return TrajectorySummary(
            trajectory_id=trajectory_id,
            tenant_id=tenant_id.strip(),
            source_task_id=task.task_id,
            schema_version=TRAJECTORY_SCHEMA_VERSION,
            status=status,
            task_summary=task_summary,
            outcome_summary=outcome_summary,
            tool_calls=tool_calls,
            evidence=tuple(evidence),
            usage=usage,
            budget=budget,
            admission=admission,
            redaction_count=redaction_count,
            created_at=utc_now(),
        )

    @staticmethod
    def _tool_calls(snapshot: RuntimeSnapshot) -> tuple[dict[str, object], ...]:
        events_by_tick: dict[str, list[object]] = {}
        for event in snapshot.events:
            events_by_tick.setdefault(event.tick_id, []).append(event)
        calls: list[dict[str, object]] = []
        for tick in snapshot.ticks:
            decision = tick.decision
            if decision.kind is not DecisionKind.TOOL_CALL:
                continue
            status = "failed"
            reason_code: str | None = None
            evidence_id: str | None = None
            for event in events_by_tick.get(tick.tick_id or "", []):
                if event.kind == "tool.succeeded":
                    status = "succeeded"
                    evidence_id = str(event.payload.get("evidence_id") or "") or None
                elif event.kind == "tool.rejected":
                    status = "rejected"
                    reason_code = str(event.payload.get("reason_code") or "") or None
            calls.append(
                {
                    "sequence": tick.sequence,
                    "tool_name": decision.tool_name or "",
                    "status": status,
                    "reason_code": reason_code,
                    "evidence_id": evidence_id,
                }
            )
        return tuple(calls)

    @staticmethod
    def _admit(
        snapshot: RuntimeSnapshot,
        *,
        final_ids: tuple[str, ...],
        evidence_by_id: dict[str, object],
        tool_calls: tuple[dict[str, object], ...],
        budget_within: bool,
    ) -> TrajectoryAdmission:
        task = snapshot.task
        task_succeeded = task.status is TaskStatus.SUCCEEDED and task.final_report is not None
        valid_final_ids = tuple(
            evidence_id
            for evidence_id in final_ids
            if evidence_id in evidence_by_id
            and bool(getattr(evidence_by_id[evidence_id], "source", "").strip())
            and bool(getattr(evidence_by_id[evidence_id], "summary", "").strip())
        )
        successful_call_evidence = tuple(
            str(item["evidence_id"])
            for item in tool_calls
            if item["status"] == "succeeded" and item.get("evidence_id")
        )
        evidence_complete = bool(final_ids) and len(valid_final_ids) == len(final_ids)
        evidence_complete = evidence_complete and all(
            evidence_id in evidence_by_id for evidence_id in successful_call_evidence
        )

        reason_codes = {
            str(event.payload.get("reason_code") or "")
            for event in snapshot.events
            if event.kind == "tool.rejected"
        }
        explicit_security_events = tuple(
            event
            for event in snapshot.events
            if event.kind.startswith("security.")
            or bool(event.payload.get("security_violation"))
        )
        security_clear = not explicit_security_events and not (
            reason_codes & _SECURITY_REASON_CODES
        )
        tool_authorized = not (reason_codes & _TOOL_OVERREACH_REASON_CODES)

        checks = {
            "task_succeeded": task_succeeded,
            "evidence_complete": evidence_complete,
            "security_clear": security_clear,
            "tool_authorized": tool_authorized,
            "within_budget": budget_within,
        }
        rejection_reasons: list[str] = []
        if not task_succeeded:
            rejection_reasons.append(TrajectoryRejectionReason.TASK_NOT_SUCCEEDED.value)
        if not evidence_complete:
            rejection_reasons.append(TrajectoryRejectionReason.EVIDENCE_INCOMPLETE.value)
        if not security_clear:
            rejection_reasons.append(TrajectoryRejectionReason.SECURITY_VIOLATION.value)
        if not tool_authorized:
            rejection_reasons.append(TrajectoryRejectionReason.TOOL_OVERREACH.value)
        if not budget_within:
            rejection_reasons.append(TrajectoryRejectionReason.BUDGET_EXCEEDED.value)

        evidence_ratio = (
            len(valid_final_ids) / len(final_ids) if final_ids else 0.0
        )
        successful_calls = sum(item["status"] == "succeeded" for item in tool_calls)
        tool_efficiency = successful_calls / len(tool_calls) if tool_calls else 0.0
        distinct_tools = len({str(item["tool_name"]) for item in tool_calls})
        reusability = min(
            1.0,
            (0.4 if len(tool_calls) >= 2 else 0.0)
            + (0.3 if distinct_tools >= 2 else 0.0)
            + (0.3 if len(valid_final_ids) >= 2 else 0.0),
        )
        failed_ticks = sum(tick.status.value == "failed" for tick in snapshot.ticks)
        safety_stability = sum(
            (
                1.0 if security_clear else 0.0,
                1.0 if tool_authorized else 0.0,
                1.0 if budget_within else 0.0,
                1.0 if failed_ticks == 0 else 0.0,
            )
        ) / 4
        factors = {
            "task_success": 1.0 if task_succeeded else 0.0,
            "evidence_completeness": round(evidence_ratio, 4),
            "tool_efficiency": round(tool_efficiency, 4),
            "reusability": round(reusability, 4),
            "safety_stability": round(safety_stability, 4),
        }
        score = round(
            0.35 * factors["task_success"]
            + 0.25 * factors["evidence_completeness"]
            + 0.15 * factors["tool_efficiency"]
            + 0.15 * factors["reusability"]
            + 0.10 * factors["safety_stability"],
            4,
        )
        explanations = (
            f"task_success={factors['task_success']:.4f} (weight=0.35)",
            f"evidence_completeness={factors['evidence_completeness']:.4f} (weight=0.25)",
            f"tool_efficiency={factors['tool_efficiency']:.4f} (weight=0.15)",
            f"reusability={factors['reusability']:.4f} (weight=0.15)",
            f"safety_stability={factors['safety_stability']:.4f} (weight=0.10)",
        )
        return TrajectoryAdmission(
            eligible=not rejection_reasons,
            rejection_reasons=tuple(rejection_reasons),
            quality_score=score,
            quality_factors=factors,
            quality_explanations=explanations,
            checks=checks,
        )


def _redact(value: str, *, limit: int = 1_600) -> tuple[str, int]:
    try:
        redacted = redact_summary(value, limit=limit)
    except Exception:
        return "[REDACTED_EMPTY]", 1
    count = redacted.count("[REDACTED_")
    redacted, path_count = _WINDOWS_PATH.subn("[REDACTED_PATH]", redacted)
    count += path_count
    redacted, path_count = _UNIX_PATH.subn("[REDACTED_PATH]", redacted)
    count += path_count
    redacted, email_count = _EMAIL.subn("[REDACTED_EMAIL]", redacted)
    count += email_count
    return redacted[:limit].rstrip(), count


def _trajectory_id(tenant_id: str, task_id: str) -> str:
    encoded = json.dumps(
        {"tenant_id": tenant_id, "task_id": task_id},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"trajectory-{hashlib.sha256(encoded).hexdigest()[:32]}"


__all__ = [
    "TRAJECTORY_SCHEMA_VERSION",
    "TrajectoryAdmission",
    "TrajectoryRejectionReason",
    "TrajectoryStatus",
    "TrajectorySummary",
    "TrajectorySummaryBuilder",
]
