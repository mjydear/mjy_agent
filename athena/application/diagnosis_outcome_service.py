"""Application seam for finalizing a tenant-scoped Diagnosis Outcome."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable

from athena.api.repositories.diagnosis_outcome_repository import (
    DiagnosisOutcome,
    DiagnosisOutcomeConflictError,
    DiagnosisOutcomeRepository,
    DiagnosticTaskNotFoundError,
    SupportingEvidenceNotFoundError,
)

_SENSITIVE_CONTENT = re.compile(
    r"(?i)(?:"
    r"<\s*(?:think|thought)\b|"
    r"\b(?:hidden[_ -]?thought|chain[_ -]?of[_ -]?thought|raw[_ -]?prompt)\b|"
    r"\b(?:api[_ -]?key|password|secret|authorization|access[_ -]?token|credential)\s*[:=]|"
    r"\bsk-[a-z0-9_-]{8,}\b"
    r")"
)


class DiagnosisOutcomeServiceError(RuntimeError):
    """Stable application error codes for API and worker callers."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        self.message = message
        super().__init__(error_code)


class DiagnosisOutcomeService:
    """Deep module that validates and finalizes exactly one outcome per task."""

    def __init__(self, repository: DiagnosisOutcomeRepository) -> None:
        self._repository = repository

    async def finalize(
        self,
        tenant_id: str,
        task_id: str,
        *,
        root_cause: str | None,
        supporting_evidence_ids: Iterable[str],
        remediation_recommendation: str | None,
        confidence: float,
        evidence_sufficient: bool,
    ) -> DiagnosisOutcome:
        """Validate a diagnosis and durably finalize it.

        A task with insufficient Evidence is still recorded as an outcome, but
        it cannot carry an asserted Root Cause or Remediation Recommendation.
        Repeating the exact request returns the existing fact; changing it is
        an explicit conflict.
        """
        tenant_id = _required_identifier(tenant_id, "tenant_id")
        task_id = _required_identifier(task_id, "task_id")
        evidence_ids = _evidence_ids(supporting_evidence_ids)
        root = _optional_text(root_cause, "root_cause")
        recommendation = _optional_text(
            remediation_recommendation, "remediation_recommendation"
        )
        _validate_confidence(confidence)
        if not isinstance(evidence_sufficient, bool):
            raise DiagnosisOutcomeServiceError(
                "OUTCOME_EVIDENCE_SUFFICIENCY_INVALID",
                "evidence_sufficient must be a boolean",
            )

        if evidence_sufficient:
            if not evidence_ids:
                raise DiagnosisOutcomeServiceError(
                    "OUTCOME_EVIDENCE_INSUFFICIENT",
                    "a sufficient Diagnosis Outcome requires supporting Evidence",
                )
            if root is None or recommendation is None:
                raise DiagnosisOutcomeServiceError(
                    "OUTCOME_CONTENT_REQUIRED",
                    "a sufficient Diagnosis Outcome requires Root Cause and Remediation Recommendation",
                )
        elif root is not None or recommendation is not None or confidence != 0.0:
            raise DiagnosisOutcomeServiceError(
                "OUTCOME_EVIDENCE_INSUFFICIENT",
                "an outcome without sufficient Evidence cannot assert a Root Cause or Remediation Recommendation",
            )

        try:
            outcome, _ = await self._repository.finalize(
                tenant_id,
                task_id,
                root_cause=root,
                supporting_evidence_ids=evidence_ids,
                remediation_recommendation=recommendation,
                confidence=float(confidence),
                evidence_sufficient=evidence_sufficient,
            )
        except DiagnosticTaskNotFoundError as exc:
            raise DiagnosisOutcomeServiceError(
                "DIAGNOSTIC_TASK_NOT_FOUND",
                "Diagnostic Task was not found for this tenant",
            ) from exc
        except SupportingEvidenceNotFoundError as exc:
            raise DiagnosisOutcomeServiceError(
                "OUTCOME_EVIDENCE_NOT_FOUND",
                "one or more supporting Evidence records do not belong to the Diagnostic Task",
            ) from exc
        except DiagnosisOutcomeConflictError as exc:
            raise DiagnosisOutcomeServiceError(
                "DIAGNOSIS_OUTCOME_CONFLICT",
                "Diagnostic Task already has a different Diagnosis Outcome",
            ) from exc
        return outcome

    async def get(self, tenant_id: str, outcome_id: str) -> DiagnosisOutcome | None:
        """Read an outcome through the same tenant-scoped fact source."""
        tenant_id = _required_identifier(tenant_id, "tenant_id")
        outcome_id = _required_identifier(outcome_id, "outcome_id")
        return await self._repository.get(tenant_id, outcome_id)


def _required_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_IDENTIFIER_INVALID", f"{field_name} must be a string"
        )
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_IDENTIFIER_INVALID", f"{field_name} is invalid"
        )
    return normalized


def _evidence_ids(values: Iterable[str]) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_EVIDENCE_IDS_INVALID",
            "supporting_evidence_ids must be an iterable of Evidence IDs",
        )
    try:
        raw_values = tuple(values)
    except TypeError as exc:
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_EVIDENCE_IDS_INVALID",
            "supporting_evidence_ids must be an iterable of Evidence IDs",
        ) from exc
    normalized: list[str] = []
    for value in raw_values:
        if not isinstance(value, str):
            raise DiagnosisOutcomeServiceError(
                "OUTCOME_EVIDENCE_IDS_INVALID",
                "each supporting Evidence ID must be a string",
            )
        item = value.strip()
        if not item or len(item) > 256:
            raise DiagnosisOutcomeServiceError(
                "OUTCOME_EVIDENCE_IDS_INVALID",
                "supporting Evidence ID is invalid",
            )
        if item in normalized:
            raise DiagnosisOutcomeServiceError(
                "OUTCOME_EVIDENCE_IDS_INVALID",
                "supporting Evidence IDs must not repeat",
            )
        normalized.append(item)
    if len(normalized) > 100:
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_EVIDENCE_IDS_INVALID",
            "at most 100 supporting Evidence IDs may be attached",
        )
    return tuple(sorted(normalized))


def _optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_CONTENT_INVALID", f"{field_name} must be a string"
        )
    normalized = value.strip()
    if not normalized or len(normalized) > 4000:
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_CONTENT_INVALID", f"{field_name} is invalid"
        )
    if _SENSITIVE_CONTENT.search(normalized):
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_SENSITIVE_CONTENT_REJECTED",
            "Outcome cannot contain raw Prompt, Secret or hidden Thought content",
        )
    return normalized


def _validate_confidence(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_CONFIDENCE_INVALID", "confidence must be a number from 0 to 1"
        )
    if not math.isfinite(float(value)) or not 0.0 <= float(value) <= 1.0:
        raise DiagnosisOutcomeServiceError(
            "OUTCOME_CONFIDENCE_INVALID", "confidence must be a number from 0 to 1"
        )
