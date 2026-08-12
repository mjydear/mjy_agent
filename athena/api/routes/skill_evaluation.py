"""Fixed Replay Case registry and Baseline APIs; no Candidate execution."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.services import ApiServiceError
from athena.application.skill_evaluation_service import SkillEvaluationService

router = APIRouter(prefix="/api/skill-evaluation", tags=["skill-evaluation"])


class BaselineRunRequest(BaseModel):
    case_ids: tuple[str, ...] = Field(default=(), max_length=12)


def _service(request: Request) -> SkillEvaluationService:
    service = getattr(request.app.state, "skill_evaluation_service", None)
    if service is None:
        raise ApiServiceError(
            "SKILL_EVALUATION_SERVICE_UNAVAILABLE",
            "Skill evaluation persistence is not configured",
            status_code=503,
        )
    return service


@router.get("/cases")
async def list_replay_cases(
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:read")),
) -> ApiResponse[dict[str, object]]:
    del tenant
    cases = _service(request).cases()
    return ApiResponse.ok(
        {
            "case_count": len(cases),
            "cases": [case.to_dict() for case in cases],
        }
    )


@router.post("/baseline-runs", status_code=status.HTTP_201_CREATED)
async def run_baseline(
    payload: BaselineRunRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        run = await _service(request).run_baseline(
            tenant.tenant_id, case_ids=payload.case_ids
        )
    except ValueError as exc:
        raise ApiServiceError(
            "SKILL_REPLAY_CASE_INVALID", str(exc), status_code=422
        ) from exc
    return ApiResponse.ok(run.to_dict())


@router.get("/baseline-runs/{run_id}")
async def get_baseline(
    run_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:read")),
) -> ApiResponse[dict[str, object]]:
    run = await _service(request).baseline(tenant.tenant_id, run_id)
    if run is None:
        raise ApiServiceError(
            "SKILL_BASELINE_RUN_NOT_FOUND", "Skill Baseline run not found", 404
        )
    return ApiResponse.ok(run.to_dict())


__all__ = ["router"]
