"""Public HTTP adapter for the inspectable Agent Runtime."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.response import ApiResponse
from athena.api.services import ApiServiceError
from athena.application.runtime_task_service import RuntimeTaskService
from athena.runtime import TaskNotFoundError

router = APIRouter(prefix="/api/runtime/tasks", tags=["agent-runtime"])


class RuntimeTaskCreateRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=8_000)
    repository_path: str = Field(min_length=1, max_length=2_000)
    profile: str | None = Field(default=None, max_length=32)


class RuntimeHumanInputRequest(BaseModel):
    input: str = Field(min_length=1, max_length=8_000)


def _service(request: Request) -> RuntimeTaskService:
    service = getattr(request.app.state, "runtime_task_service", None)
    if service is None:
        raise ApiServiceError("RUNTIME_UNAVAILABLE", "Agent Runtime is unavailable", 503)
    return service


def _not_found() -> ApiServiceError:
    return ApiServiceError("RUNTIME_TASK_NOT_FOUND", "Runtime task was not found", 404)


@router.post("")
async def create_task(
    payload: RuntimeTaskCreateRequest,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:run")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).create(
            goal=payload.goal,
            repository_path=payload.repository_path,
            profile=payload.profile,
        )
    except ValueError as exc:
        code = str(exc)
        raise ApiServiceError(code, "repository path or task profile is invalid", 422) from exc
    return ApiResponse.ok(result)


@router.get("")
async def list_tasks(
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    return ApiResponse.ok(_service(request).list())


@router.get("/{task_id}")
async def get_task(
    task_id: str,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).detail(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    return ApiResponse.ok(result)

@router.post("/{task_id}/run")
async def run_task(
    task_id: str,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:run")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).run(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    return ApiResponse.ok(result)


@router.post("/{task_id}/input")
async def supply_human_input(
    task_id: str,
    payload: RuntimeHumanInputRequest,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:run")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).supply_human_input(task_id, payload.input)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    except ValueError as exc:
        raise ApiServiceError("RUNTIME_INPUT_REJECTED", str(exc), 409) from exc
    return ApiResponse.ok(result)


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:cancel")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).cancel(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    return ApiResponse.ok(result)


@router.get("/{task_id}/events")
async def events(
    task_id: str,
    request: Request,
    after: int = Query(default=0, ge=0),
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).events(task_id, after)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    return ApiResponse.ok(result)


@router.get("/{task_id}/evidence")
async def evidence(
    task_id: str,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).evidence(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    return ApiResponse.ok(result)


@router.get("/{task_id}/context")
async def context(
    task_id: str,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).context(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    return ApiResponse.ok(result)


@router.get("/{task_id}/usage")
async def usage(
    task_id: str,
    request: Request,
    _: TenantContext = Depends(require_scope("runtime:read")),
) -> ApiResponse[dict[str, object]]:
    try:
        result = _service(request).usage(task_id)
    except TaskNotFoundError as exc:
        raise _not_found() from exc
    return ApiResponse.ok(result)
