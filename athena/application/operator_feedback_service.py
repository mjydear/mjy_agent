"""Application seam for recording Operator Feedback and optional Recovery."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime

from athena.api.repositories.diagnosis_outcome_repository import (
    DiagnosisOutcomeNotFoundError,
    DiagnosisOutcomeRepository,
    FeedbackIdempotencyConflictError,
    OperatorFeedback,
)

_FEEDBACK_TYPES = frozenset({"confirmed", "corrected", "rejected"})
_SENSITIVE_CONTENT = re.compile(
    r"(?i)(?:"
    r"<\s*(?:think|thought)\b|"
    r"\b(?:hidden[_ -]?thought|chain[_ -]?of[_ -]?thought|raw[_ -]?prompt)\b|"
    r"\b(?:api[_ -]?key|password|secret|authorization|access[_ -]?token|credential)\s*[:=]|"
    r"\bsk-[a-z0-9_-]{8,}\b"
    r")"
)


@dataclass(frozen=True)
class RecoveryObservation:
    """Operator-observed return to an acceptable workload condition."""

    observed_at: datetime
    summary: str


class OperatorFeedbackServiceError(RuntimeError):
    """Stable application error codes for feedback callers."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(error_code)


class OperatorFeedbackService:
    """Deep module enforcing feedback semantics and idempotent persistence."""

    def __init__(self, repository: DiagnosisOutcomeRepository) -> None:
        self._repository = repository

    async def record(
        self,
        tenant_id: str,
        task_id: str,
        outcome_id: str,
        *,
        feedback_type: str,
        idempotency_key: str,
        operator_id: str = "unknown",
        corrected_root_cause: str | None = None,
        corrected_remediation_recommendation: str | None = None,
        note: str | None = None,
        recovery: RecoveryObservation | None = None,
    ) -> OperatorFeedback:
        """Record one assessment without mutating the task checkpoint.

        The idempotency key is tenant-scoped.  Reusing it with the same
        normalized request returns the original feedback; changing any field
        produces a conflict instead of a second fact.
        """
        tenant_id = _identifier(tenant_id, "tenant_id")
        task_id = _identifier(task_id, "task_id")
        outcome_id = _identifier(outcome_id, "outcome_id")
        idempotency_key = _identifier(idempotency_key, "idempotency_key")
        operator_id = _text(operator_id, "operator_id", limit=160, required=True)
        feedback_type = _feedback_type(feedback_type)
        corrected_root_cause = _text(
            corrected_root_cause, "corrected_root_cause", limit=4000
        )
        corrected_remediation_recommendation = _text(
            corrected_remediation_recommendation,
            "corrected_remediation_recommendation",
            limit=4000,
        )
        note = _text(note, "note", limit=2000)
        _validate_correction(
            feedback_type,
            corrected_root_cause,
            corrected_remediation_recommendation,
        )
        recovery_at, recovery_summary = _recovery_values(recovery)
        request_hash = _request_hash(
            feedback_type=feedback_type,
            operator_id=operator_id,
            corrected_root_cause=corrected_root_cause,
            corrected_remediation_recommendation=corrected_remediation_recommendation,
            note=note,
            recovery_observed_at=recovery_at,
            recovery_summary=recovery_summary,
        )

        try:
            feedback, _ = await self._repository.record_feedback(
                tenant_id,
                task_id,
                outcome_id,
                feedback_type=feedback_type,
                corrected_root_cause=corrected_root_cause,
                corrected_remediation_recommendation=(
                    corrected_remediation_recommendation
                ),
                note=note,
                submitted_by=operator_id,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                recovery_observed_at=recovery_at,
                recovery_summary=recovery_summary,
            )
        except DiagnosisOutcomeNotFoundError as exc:
            raise OperatorFeedbackServiceError(
                "DIAGNOSIS_OUTCOME_NOT_FOUND",
                "Diagnosis Outcome was not found for this tenant and task",
            ) from exc
        except FeedbackIdempotencyConflictError as exc:
            raise OperatorFeedbackServiceError(
                "FEEDBACK_IDEMPOTENCY_CONFLICT",
                "feedback idempotency key was already used for different content",
            ) from exc
        return feedback


def _identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise OperatorFeedbackServiceError(
            "FEEDBACK_IDENTIFIER_INVALID", f"{field_name} must be a string"
        )
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise OperatorFeedbackServiceError(
            "FEEDBACK_IDENTIFIER_INVALID", f"{field_name} is invalid"
        )
    return normalized


def _text(
    value: str | None,
    field_name: str,
    *,
    limit: int,
    required: bool = False,
) -> str | None:
    if value is None:
        if required:
            raise OperatorFeedbackServiceError(
                "FEEDBACK_CONTENT_INVALID", f"{field_name} is required"
            )
        return None
    if not isinstance(value, str):
        raise OperatorFeedbackServiceError(
            "FEEDBACK_CONTENT_INVALID", f"{field_name} must be a string"
        )
    normalized = value.strip()
    if not normalized:
        if required:
            raise OperatorFeedbackServiceError(
                "FEEDBACK_CONTENT_INVALID", f"{field_name} is required"
            )
        return None
    if len(normalized) > limit:
        raise OperatorFeedbackServiceError(
            "FEEDBACK_CONTENT_INVALID", f"{field_name} exceeds its length limit"
        )
    if _SENSITIVE_CONTENT.search(normalized):
        raise OperatorFeedbackServiceError(
            "FEEDBACK_SENSITIVE_CONTENT_REJECTED",
            "Operator Feedback cannot contain raw Prompt, Secret or hidden Thought content",
        )
    return normalized


def _feedback_type(value: str) -> str:
    if not isinstance(value, str):
        raise OperatorFeedbackServiceError(
            "FEEDBACK_TYPE_INVALID", "feedback_type is invalid"
        )
    normalized = value.strip().lower()
    if normalized not in _FEEDBACK_TYPES:
        raise OperatorFeedbackServiceError(
            "FEEDBACK_TYPE_INVALID",
            "feedback_type must be confirmed, corrected or rejected",
        )
    return normalized


def _validate_correction(
    feedback_type: str,
    corrected_root_cause: str | None,
    corrected_remediation_recommendation: str | None,
) -> None:
    has_correction = bool(
        corrected_root_cause or corrected_remediation_recommendation
    )
    if feedback_type == "corrected" and not has_correction:
        raise OperatorFeedbackServiceError(
            "FEEDBACK_CORRECTION_REQUIRED",
            "corrected feedback requires a corrected Root Cause or Remediation Recommendation",
        )
    if feedback_type != "corrected" and has_correction:
        raise OperatorFeedbackServiceError(
            "FEEDBACK_CORRECTION_NOT_ALLOWED",
            "correction fields are only allowed for corrected feedback",
        )


def _recovery_values(
    recovery: RecoveryObservation | None,
) -> tuple[datetime | None, str | None]:
    if recovery is None:
        return None, None
    if not isinstance(recovery, RecoveryObservation):
        raise OperatorFeedbackServiceError(
            "FEEDBACK_RECOVERY_INVALID",
            "recovery must be a RecoveryObservation",
        )
    if not isinstance(recovery.observed_at, datetime):
        raise OperatorFeedbackServiceError(
            "FEEDBACK_RECOVERY_INVALID", "recovery observed_at is invalid"
        )
    observed_at = (
        recovery.observed_at.replace(tzinfo=UTC)
        if recovery.observed_at.tzinfo is None
        else recovery.observed_at.astimezone(UTC)
    )
    summary = _text(recovery.summary, "recovery.summary", limit=2000, required=True)
    assert summary is not None
    return observed_at, summary


def _request_hash(
    *,
    feedback_type: str,
    operator_id: str,
    corrected_root_cause: str | None,
    corrected_remediation_recommendation: str | None,
    note: str | None,
    recovery_observed_at: datetime | None,
    recovery_summary: str | None,
) -> str:
    payload = {
        "corrected_remediation_recommendation": corrected_remediation_recommendation,
        "corrected_root_cause": corrected_root_cause,
        "feedback_type": feedback_type,
        "operator_id": operator_id,
        "note": note,
        "recovery_observed_at": (
            recovery_observed_at.isoformat() if recovery_observed_at else None
        ),
        "recovery_summary": recovery_summary,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
