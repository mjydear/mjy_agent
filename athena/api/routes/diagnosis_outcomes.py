"""Independent APIs for Diagnosis Outcome and Operator Feedback facts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, Header, Request, status
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.services import ApiServiceError
from athena.application.diagnosis_outcome_service import (
    DiagnosisOutcomeService,
    DiagnosisOutcomeServiceError,
)
from athena.application.operator_feedback_service import (
    OperatorFeedbackService,
    OperatorFeedbackServiceError,
    RecoveryObservation,
)
from athena.api.repositories.diagnosis_outcome_repository import (
    DiagnosisOutcome,
    OperatorFeedback,
    Recovery,
)

router = APIRouter(prefix="/api/diagnosis-outcomes", tags=["diagnosis-outcomes"])


class DiagnosisOutcomeFinalizeRequest(BaseModel):
    root_cause: str | None = Field(default=None, max_length=4000)
    supporting_evidence_ids: list[str] = Field(default_factory=list, max_length=100)
    remediation_recommendation: str | None = Field(default=None, max_length=4000)
    confidence: float = Field(ge=0, le=1)
    evidence_sufficient: bool


class OperatorFeedbackRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=256)
    feedback_type: Literal["confirmed", "corrected", "rejected"]
    corrected_root_cause: str | None = Field(default=None, max_length=4000)
    corrected_remediation_recommendation: str | None = Field(
        default=None, max_length=4000
    )
    note: str | None = Field(default=None, max_length=2000)
    recovery_observed_at: datetime | None = None
    recovery_summary: str | None = Field(default=None, max_length=2000)


def _outcome_service(request: Request) -> DiagnosisOutcomeService:
    service = getattr(request.app.state, "diagnosis_outcome_service", None)
    if service is None:
        raise ApiServiceError(
            "DIAGNOSIS_OUTCOME_SERVICE_UNAVAILABLE",
            "Diagnosis Outcome persistence is not configured",
            status_code=503,
        )
    return service


def _feedback_service(request: Request) -> OperatorFeedbackService:
    service = getattr(request.app.state, "operator_feedback_service", None)
    if service is None:
        raise ApiServiceError(
            "OPERATOR_FEEDBACK_SERVICE_UNAVAILABLE",
            "Operator Feedback persistence is not configured",
            status_code=503,
        )
    return service


def _outcome_error(exc: DiagnosisOutcomeServiceError) -> ApiServiceError:
    statuses = {
        "DIAGNOSTIC_TASK_NOT_FOUND": 404,
        "DIAGNOSIS_OUTCOME_CONFLICT": 409,
        "OUTCOME_EVIDENCE_NOT_FOUND": 422,
        "OUTCOME_EVIDENCE_INSUFFICIENT": 422,
        "OUTCOME_CONTENT_REQUIRED": 400,
        "OUTCOME_SENSITIVE_CONTENT_REJECTED": 400,
    }
    return ApiServiceError(
        exc.error_code,
        exc.message,
        status_code=statuses.get(exc.error_code, 400),
    )


def _feedback_error(exc: OperatorFeedbackServiceError) -> ApiServiceError:
    statuses = {
        "DIAGNOSIS_OUTCOME_NOT_FOUND": 404,
        "FEEDBACK_IDEMPOTENCY_CONFLICT": 409,
        "FEEDBACK_SENSITIVE_CONTENT_REJECTED": 400,
    }
    return ApiServiceError(
        exc.error_code,
        exc.message,
        status_code=statuses.get(exc.error_code, 400),
    )


def _recovery_view(recovery: Recovery | None) -> dict[str, object] | None:
    if recovery is None:
        return None
    return {
        "id": recovery.recovery_id,
        "task_id": recovery.task_id,
        "outcome_id": recovery.outcome_id,
        "feedback_id": recovery.feedback_id,
        "observed_at": recovery.observed_at,
        "summary": recovery.summary,
    }


def _outcome_view(outcome: DiagnosisOutcome) -> dict[str, object]:
    return {
        "id": outcome.outcome_id,
        "task_id": outcome.task_id,
        "root_cause": outcome.root_cause,
        "supporting_evidence_ids": list(outcome.supporting_evidence_ids),
        "remediation_recommendation": outcome.remediation_recommendation,
        "confidence": outcome.confidence,
        "evidence_sufficient": outcome.evidence_sufficient,
        "finalized_at": outcome.finalized_at,
    }


def _feedback_view(feedback: OperatorFeedback) -> dict[str, object]:
    return {
        "id": feedback.feedback_id,
        "task_id": feedback.task_id,
        "outcome_id": feedback.outcome_id,
        "feedback_type": feedback.feedback_type,
        "corrected_root_cause": feedback.corrected_root_cause,
        "corrected_remediation_recommendation": (
            feedback.corrected_remediation_recommendation
        ),
        "note": feedback.note,
        "submitted_by": feedback.submitted_by,
        "idempotency_key": feedback.idempotency_key,
        "created_at": feedback.created_at,
        "recovery": _recovery_view(feedback.recovery),
    }


@router.post(
    "/tasks/{task_id}/finalize",
    status_code=status.HTTP_201_CREATED,
)
async def finalize_outcome(
    task_id: str,
    payload: DiagnosisOutcomeFinalizeRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("diagnosis:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        outcome = await _outcome_service(request).finalize(
            tenant.tenant_id,
            task_id,
            root_cause=payload.root_cause,
            supporting_evidence_ids=payload.supporting_evidence_ids,
            remediation_recommendation=payload.remediation_recommendation,
            confidence=payload.confidence,
            evidence_sufficient=payload.evidence_sufficient,
        )
    except DiagnosisOutcomeServiceError as exc:
        raise _outcome_error(exc) from exc
    return ApiResponse.ok(_outcome_view(outcome))


@router.get("/{outcome_id}")
async def get_outcome(
    outcome_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("diagnosis:read")),
) -> ApiResponse[dict[str, object]]:
    outcome = await _outcome_service(request).get(tenant.tenant_id, outcome_id)
    if outcome is None:
        raise ApiServiceError(
            "DIAGNOSIS_OUTCOME_NOT_FOUND", "Diagnosis Outcome was not found", 404
        )
    return ApiResponse.ok(_outcome_view(outcome))


@router.post(
    "/{outcome_id}/feedback",
    status_code=status.HTTP_201_CREATED,
)
async def record_feedback(
    outcome_id: str,
    payload: OperatorFeedbackRequest,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    tenant: TenantContext = Depends(require_scope("diagnosis:feedback")),
) -> ApiResponse[dict[str, object]]:
    if not idempotency_key or not idempotency_key.strip():
        raise ApiServiceError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header is required for Operator Feedback",
            status_code=400,
        )
    if (payload.recovery_observed_at is None) != (
        payload.recovery_summary is None
    ):
        raise ApiServiceError(
            "FEEDBACK_RECOVERY_INVALID",
            "recovery_observed_at and recovery_summary must be supplied together",
            status_code=400,
        )
    try:
        feedback = await _feedback_service(request).record(
            tenant.tenant_id,
            payload.task_id,
            outcome_id,
            feedback_type=payload.feedback_type,
            idempotency_key=idempotency_key,
            operator_id=tenant.tenant_id,
            corrected_root_cause=payload.corrected_root_cause,
            corrected_remediation_recommendation=(
                payload.corrected_remediation_recommendation
            ),
            note=payload.note,
            recovery=(
                RecoveryObservation(
                    observed_at=payload.recovery_observed_at,
                    summary=payload.recovery_summary,
                )
                if payload.recovery_observed_at is not None
                else None
            ),
        )
    except OperatorFeedbackServiceError as exc:
        raise _feedback_error(exc) from exc
    return ApiResponse.ok(_feedback_view(feedback))
