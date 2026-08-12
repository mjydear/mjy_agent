"""Public, human-gated Runtime Skill learning endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.services import ApiServiceError
from athena.application.runtime_learning_service import RuntimeLearningService
from athena.runtime.learning import RuntimeSkillLearningError
from athena.runtime.store import TaskNotFoundError

router = APIRouter(prefix="/api/runtime/skills", tags=["agent-runtime-learning"])


class RuntimeFeedbackRequest(BaseModel):
    feedback_id: str = Field(min_length=1, max_length=160)
    accepted: bool
    verified: bool
    summary: str = Field(min_length=1, max_length=2_000)
    submitted_by: str = Field(min_length=1, max_length=160)


class RuntimeEvaluationRequest(BaseModel):
    cases: list[dict[str, object]] = Field(min_length=1, max_length=32)


class RuntimeReviewRequest(BaseModel):
    reviewer: str = Field(min_length=1, max_length=160)
    approved: bool
    note: str = Field(min_length=1, max_length=2_000)


def _service(request: Request) -> RuntimeLearningService:
    service = getattr(request.app.state, "runtime_learning_service", None)
    if service is None:
        raise ApiServiceError("RUNTIME_LEARNING_UNAVAILABLE", "Runtime learning is unavailable", 503)
    return service


def _learning_error(exc: RuntimeSkillLearningError) -> ApiServiceError:
    status = 404 if exc.error_code == "RUNTIME_SKILL_CANDIDATE_NOT_FOUND" else 409
    return ApiServiceError(exc.error_code, "Skill candidate lifecycle operation was rejected", status)


@router.get("")
async def list_candidates(
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    return ApiResponse.ok(_service(request).list())


@router.get("/{candidate_id}")
async def get_candidate(
    candidate_id: str,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    try:
        return ApiResponse.ok(_service(request).detail(candidate_id))
    except RuntimeSkillLearningError as exc:
        raise _learning_error(exc) from exc


@router.post("/tasks/{task_id}/feedback")
async def observe_feedback(
    task_id: str,
    payload: RuntimeFeedbackRequest,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:learn")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).observe(task_id, **payload.model_dump())
    except TaskNotFoundError as exc:
        raise ApiServiceError("RUNTIME_TASK_NOT_FOUND", "Runtime task was not found", 404) from exc
    except RuntimeSkillLearningError as exc:
        raise _learning_error(exc) from exc
    return ApiResponse.ok(result)


@router.post("/{candidate_id}/replay")
async def replay_candidate(
    candidate_id: str,
    payload: RuntimeEvaluationRequest,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:learn")),
) -> ApiResponse[dict[str, object]]:
    try:
        return ApiResponse.ok(_service(request).replay(candidate_id, payload.cases))
    except RuntimeSkillLearningError as exc:
        raise _learning_error(exc) from exc


@router.post("/{candidate_id}/shadow")
async def shadow_candidate(
    candidate_id: str,
    payload: RuntimeEvaluationRequest,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:learn")),
) -> ApiResponse[dict[str, object]]:
    try:
        return ApiResponse.ok(_service(request).shadow(candidate_id, payload.cases))
    except RuntimeSkillLearningError as exc:
        raise _learning_error(exc) from exc


@router.post("/{candidate_id}/review")
async def review_candidate(
    candidate_id: str,
    payload: RuntimeReviewRequest,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:learn")),
) -> ApiResponse[dict[str, object]]:
    try:
        return ApiResponse.ok(_service(request).review(candidate_id, **payload.model_dump()))
    except RuntimeSkillLearningError as exc:
        raise _learning_error(exc) from exc


@router.post("/{candidate_id}/handoff")
async def handoff_candidate(
    candidate_id: str,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:learn")),
) -> ApiResponse[dict[str, object]]:
    try:
        return ApiResponse.ok(_service(request).handoff(candidate_id))
    except RuntimeSkillLearningError as exc:
        raise _learning_error(exc) from exc
