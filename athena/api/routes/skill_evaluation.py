"""Fixed Replay Case registry and Baseline APIs; no Candidate execution."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.errors import ApiServiceError
from athena.application.shadow_traffic_ingress import (
    ShadowTrafficIngressAdapter,
    ShadowTrafficIngressError,
)
from athena.application.shadow_traffic_service import ShadowTrafficService
from athena.application.skill_evaluation_service import SkillEvaluationService
from athena.learning.skill_candidate import SkillCandidateLifecycleError

router = APIRouter(prefix="/api/skill-evaluation", tags=["skill-evaluation"])


class BaselineRunRequest(BaseModel):
    case_ids: tuple[str, ...] = Field(default=(), max_length=12)


class ShadowTrafficCaptureRequest(BaseModel):
    task_id: str = Field(min_length=1, max_length=96)
    trace_id: str = Field(min_length=1, max_length=128)
    traceparent: str | None = Field(default=None, max_length=256)


def _service(request: Request) -> SkillEvaluationService:
    service = getattr(request.app.state, "skill_evaluation_service", None)
    if service is None:
        raise ApiServiceError(
            "SKILL_EVALUATION_SERVICE_UNAVAILABLE",
            "Skill evaluation persistence is not configured",
            status_code=503,
        )
    return service


def _traffic_service(request: Request) -> ShadowTrafficService:
    service = getattr(request.app.state, "shadow_traffic_service", None)
    if service is None:
        raise ApiServiceError(
            "SHADOW_TRAFFIC_SERVICE_UNAVAILABLE",
            "Shadow traffic capture is not configured",
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


@router.post(
    "/candidates/{candidate_id}/replay-ab-runs",
    status_code=status.HTTP_201_CREATED,
)
async def run_candidate_replay_ab(
    candidate_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        run = await _service(request).run_replay_ab(tenant.tenant_id, candidate_id)
    except SkillCandidateLifecycleError as exc:
        status_code = 404 if exc.error_code == "SKILL_CANDIDATE_NOT_FOUND" else 409
        if exc.error_code == "SKILL_CANDIDATE_REPLAY_AB_UNAVAILABLE":
            status_code = 503
        raise ApiServiceError(
            exc.error_code,
            "Candidate Replay A/B precondition failed",
            status_code,
        ) from exc
    return ApiResponse.ok(run.to_dict())


@router.get("/replay-ab-runs/{run_id}")
async def get_candidate_replay_ab(
    run_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:read")),
) -> ApiResponse[dict[str, object]]:
    run = await _service(request).replay_ab(tenant.tenant_id, run_id)
    if run is None:
        raise ApiServiceError(
            "SKILL_REPLAY_AB_RUN_NOT_FOUND",
            "Skill Replay A/B run not found",
            404,
        )
    return ApiResponse.ok(run.to_dict())


@router.post(
    "/candidates/{candidate_id}/shadow-runs",
    status_code=status.HTTP_201_CREATED,
)
async def run_candidate_shadow(
    candidate_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        run = await _service(request).run_shadow(tenant.tenant_id, candidate_id)
    except SkillCandidateLifecycleError as exc:
        status_code = 404 if exc.error_code == "SKILL_CANDIDATE_NOT_FOUND" else 409
        if exc.error_code == "SKILL_CANDIDATE_SHADOW_UNAVAILABLE":
            status_code = 503
        raise ApiServiceError(
            exc.error_code,
            "Candidate Shadow precondition failed",
            status_code,
        ) from exc
    return ApiResponse.ok(run.to_dict())


@router.get("/shadow-runs/{run_id}")
async def get_candidate_shadow(
    run_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:read")),
) -> ApiResponse[dict[str, object]]:
    run = await _service(request).shadow(tenant.tenant_id, run_id)
    if run is None:
        raise ApiServiceError(
            "SKILL_SHADOW_RUN_NOT_FOUND",
            "Skill Shadow run not found",
            404,
        )
    return ApiResponse.ok(run.to_dict())


@router.post(
    "/candidates/{candidate_id}/shadow-traffic/captures",
    status_code=status.HTTP_201_CREATED,
)
async def capture_shadow_traffic(
    candidate_id: str,
    payload: ShadowTrafficCaptureRequest,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    try:
        observation = await _traffic_service(request).capture_runtime_task(
            tenant.tenant_id,
            candidate_id,
            payload.task_id,
            payload.trace_id,
            traceparent=payload.traceparent,
        )
    except SkillCandidateLifecycleError as exc:
        status_code = (
            404
            if exc.error_code
            in {
                "SKILL_CANDIDATE_NOT_FOUND",
                "RUNTIME_TRACE_NOT_FOUND",
            }
            else 409
        )
        if exc.error_code == "SHADOW_TRAFFIC_SERVICE_UNAVAILABLE":
            status_code = 503
        raise ApiServiceError(
            exc.error_code,
            "Shadow traffic capture precondition failed",
            status_code,
        ) from exc
    return ApiResponse.ok(observation.to_dict())


@router.post(
    "/candidates/{candidate_id}/shadow-traffic/ingress",
    status_code=status.HTTP_201_CREATED,
)
async def ingest_shadow_traffic(
    candidate_id: str,
    payload: dict[str, object],
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:write")),
) -> ApiResponse[dict[str, object]]:
    """Accept only a bounded Runtime completion reference for Shadow capture."""

    adapter = ShadowTrafficIngressAdapter(
        _traffic_service(request),
        candidate_id=candidate_id,
        source="runtime.agent",
        allowed_tenants=(tenant.tenant_id,),
    )
    try:
        observation = await adapter.ingest(payload)
    except ShadowTrafficIngressError as exc:
        raise ApiServiceError(
            exc.error_code,
            "Shadow traffic ingress payload was rejected",
            422,
        ) from exc
    except SkillCandidateLifecycleError as exc:
        status_code = 404 if exc.error_code == "RUNTIME_TRACE_NOT_FOUND" else 409
        raise ApiServiceError(
            exc.error_code,
            "Shadow traffic ingress precondition failed",
            status_code,
        ) from exc
    return ApiResponse.ok(observation.to_dict())


@router.get("/shadow-traffic/{observation_id}")
async def get_shadow_traffic(
    observation_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("skills:read")),
) -> ApiResponse[dict[str, object]]:
    observation = await _traffic_service(request).get(tenant.tenant_id, observation_id)
    if observation is None:
        raise ApiServiceError(
            "SHADOW_TRAFFIC_NOT_FOUND", "Shadow traffic observation not found", 404
        )
    return ApiResponse.ok(observation.to_dict())


__all__ = ["router"]
