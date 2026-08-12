"""
📦 异步任务 API 路由
📍 架构位置：HTTP 路由层，提供"提交任务→拿 task_id→轮询结果"的异步接口。
🎯 核心作用：把耗时的对话/工作流任务改为异步执行，避免长请求阻塞连接池；
             集成 API Key 鉴权、多租户隔离、Idempotency-Key 幂等。
🔗 依赖：AthenaWebService / AsyncTaskManager / IdempotencyManager / TenantContext。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request

from athena.api.auth import TenantContext, require_tenant
from athena.api.idempotency import IdempotencyManager, get_idempotency_key
from athena.api.response import ApiResponse
from athena.api.routes._deps import get_idempotency, get_service, get_task_manager
from athena.api.schemas import (
    TaskStatusResponse,
    TaskSubmitRequest,
    TaskSubmitResponse,
)
from athena.api.services import ApiServiceError, AthenaWebService
from athena.api.task_manager import AsyncTaskManager

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _build_factory(
    service: AthenaWebService,
    request: TaskSubmitRequest,
    tenant: TenantContext,
) -> Any:
    """根据任务类型构造后台协程工厂，返回可序列化结果 dict。"""
    if request.kind == "chat":
        if not request.session_id or not request.message:
            raise ApiServiceError(
                "INVALID_TASK", "chat task requires session_id and message"
            )
        session_id = request.session_id
        message = request.message

        async def chat_factory() -> dict[str, Any]:
            resp = await service.chat(session_id, message, tenant=tenant)
            return resp.model_dump()

        return chat_factory

    if request.kind == "workflow":
        if not request.task:
            raise ApiServiceError("INVALID_TASK", "workflow task requires task text")
        task = request.task
        workflow_type = request.workflow_type

        async def workflow_factory() -> dict[str, Any]:
            resp = await service.run_workflow(task, workflow_type, tenant=tenant)
            return resp.model_dump()

        return workflow_factory

    raise ApiServiceError("INVALID_TASK", f"unsupported task kind: {request.kind}")


@router.post("", response_model=ApiResponse[TaskSubmitResponse])
async def submit_task(
    payload: TaskSubmitRequest,
    request: Request,
    service: AthenaWebService = Depends(get_service),
    manager: AsyncTaskManager = Depends(get_task_manager),
    idempotency: IdempotencyManager = Depends(get_idempotency),
    tenant: TenantContext = Depends(require_tenant),
) -> ApiResponse[TaskSubmitResponse]:
    """
    提交一个异步任务，立即返回 task_id（不等待执行完成）。

    幂等：携带 Idempotency-Key 头时，重复提交返回同一个 task_id。
    """
    idem_key = get_idempotency_key(request)
    cached = idempotency.lookup(tenant.tenant_id, idem_key)
    if cached is not None:
        return ApiResponse.ok(TaskSubmitResponse(**cached))

    factory = _build_factory(service, payload, tenant)
    task_id = manager.submit(factory, tenant_id=tenant.tenant_id, kind=payload.kind)
    result = TaskSubmitResponse(task_id=task_id, status="pending")
    idempotency.remember(tenant.tenant_id, idem_key, result.model_dump())
    return ApiResponse.ok(result)


@router.get("/{task_id}", response_model=ApiResponse[TaskStatusResponse])
async def get_task(
    task_id: str,
    manager: AsyncTaskManager = Depends(get_task_manager),
    tenant: TenantContext = Depends(require_tenant),
) -> ApiResponse[TaskStatusResponse]:
    """轮询任务状态与结果；跨租户访问视为不存在（404）。"""
    record = manager.get(task_id, tenant_id=tenant.tenant_id)
    if record is None:
        raise ApiServiceError("TASK_NOT_FOUND", "task not found", status_code=404)
    return ApiResponse.ok(
        TaskStatusResponse(
            task_id=record.task_id,
            kind=record.kind,
            status=record.status,
            result=record.result,
            error=record.error,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
    )
