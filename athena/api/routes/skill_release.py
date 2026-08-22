"""Human-gated Candidate release and explicit Skill rollback APIs."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.errors import ApiServiceError
from athena.application.skill_release_service import (
    SkillReleaseError,
    SkillReleaseResult,
    SkillReleaseService,
)

router = APIRouter(prefix="/api/skill-release", tags=["skill-release"])


class ReleaseRequest(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=160)
    note: str = Field(default="", max_length=2_000)


class RollbackRequest(BaseModel):
    skill_id: str = Field(min_length=1, max_length=96)
    target_version_id: str = Field(min_length=1, max_length=96)
    reviewed_by: str = Field(min_length=1, max_length=160)
    note: str = Field(min_length=1, max_length=2_000)


def _service(request: Request) -> SkillReleaseService:
    service = getattr(request.app.state, "skill_release_service", None)
    if service is None:
        raise ApiServiceError(
            "SKILL_RELEASE_SERVICE_UNAVAILABLE",
            "Skill release lifecycle is not configured",
            status_code=503,
        )
    return service


def _release_view(result: SkillReleaseResult) -> dict[str, object]:
    version = result.version
    return {
        "release_id": result.release_id,
        "tenant_id": result.tenant_id,
        "candidate_id": result.candidate_id,
        "activation_allowed": result.activation_allowed,
        "reviewed_by": result.reviewed_by,
        "validation_report_id": result.validation_report_id,
        "replay_report_id": result.replay_report_id,
        "shadow_report_id": result.shadow_report_id,
        "gate_snapshot": result.gate_snapshot,
        "version": {
            "version_id": version.version_id,
            "skill_id": version.skill_id,
            "version": version.version,
            "status": version.status,
            "manifest": version.manifest,
            "procedure": version.procedure,
            "checksum": version.checksum,
            "source_task_id": version.source_task_id,
            "benchmark_report_id": version.benchmark_report_id,
            "created_by": version.created_by,
            "reviewed_by": version.reviewed_by,
            "review_note": version.review_note,
        },
    }


def _release_error(exc: SkillReleaseError) -> ApiServiceError:
    not_found = {
        "SKILL_RELEASE_CANDIDATE_NOT_FOUND",
        "SKILL_RELEASE_ROLLBACK_TARGET_NOT_FOUND",
    }
    status_code = 404 if exc.error_code in not_found else 409
    return ApiServiceError(
        exc.error_code, "Skill release lifecycle operation was rejected", status_code
    )


@router.post(
    "/candidates/{candidate_id}/release",
    status_code=status.HTTP_201_CREATED,
)
async def release_candidate(
    candidate_id: str,
    payload: ReleaseRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = await _service(request).release(
            tenant.tenant_id,
            candidate_id,
            reviewed_by=payload.reviewed_by,
            note=payload.note,
        )
    except SkillReleaseError as exc:
        raise _release_error(exc) from exc
    return ApiResponse.ok(_release_view(result))


@router.post("/rollback", status_code=status.HTTP_200_OK)
async def rollback_skill(
    payload: RollbackRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = await _service(request).rollback(
            tenant.tenant_id,
            skill_id=payload.skill_id,
            target_version_id=payload.target_version_id,
            reviewed_by=payload.reviewed_by,
            note=payload.note,
        )
    except SkillReleaseError as exc:
        raise _release_error(exc) from exc
    return ApiResponse.ok(_release_view(result))


__all__ = ["router"]
