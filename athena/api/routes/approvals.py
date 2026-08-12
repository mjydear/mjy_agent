"""OperationPlan and Approval APIs for controlled write boundaries."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel, Field

from athena.api.auth import TenantContext
from athena.api.rbac import require_scope
from athena.api.repositories.operation_plan_repository import (
    Approval,
    ApprovalRepository,
    OperationPlan,
    OperationPlanRepository,
)
from athena.api.repositories.tool_effect_repository import (
    ToolEffectConflictError,
    ToolEffectRepository,
)
from athena.api.response import ApiResponse
from athena.api.services import ApiServiceError
from athena.application.approval_service import ApprovalService, ApprovalServiceError
from athena.tools.cloud.k8s.actions import (
    K8sActionPlan,
    K8sActionSecurityPolicy,
    K8sWriteActionExecutor,
)

plans_router = APIRouter(prefix="/api/operation-plans", tags=["operation-plans"])
approvals_router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class OperationPlanCreate(BaseModel):
    task_id: str | None = Field(default=None, max_length=80)
    environment_id: str = Field(min_length=1, max_length=120)
    action_type: str = Field(min_length=1, max_length=120)
    resource_kind: str = Field(min_length=1, max_length=80)
    resource_name: str = Field(min_length=1, max_length=160)
    namespace: str = Field(min_length=1, max_length=120)
    risk_level: Literal["S3", "S4"] = "S3"
    required_scope: str = Field(default="cloud:execute", min_length=1, max_length=120)
    parameters: dict[str, object] = Field(default_factory=dict)
    preconditions: dict[str, object] = Field(default_factory=dict)
    postconditions: dict[str, object] = Field(default_factory=dict)
    rollback: dict[str, object] = Field(default_factory=dict)
    dry_run: dict[str, object] = Field(default_factory=dict)
    expires_in_seconds: int | None = Field(default=3600, ge=60, le=86_400)


class ApprovalDecision(BaseModel):
    plan_hash: str = Field(min_length=64, max_length=128)
    note: str | None = Field(default=None, max_length=500)


class ApprovalReject(BaseModel):
    note: str | None = Field(default=None, max_length=500)


class OperationPlanExecute(BaseModel):
    approval_id: str = Field(min_length=1, max_length=80)
    plan_hash: str = Field(min_length=64, max_length=128)


def _plan_repo(request: Request) -> OperationPlanRepository:
    repo = getattr(request.app.state, "operation_plan_repository", None)
    if repo is None:
        raise ApiServiceError(
            "PLAN_STORE_UNAVAILABLE",
            "operation plan persistence is not configured",
            503,
        )
    return repo


def _approval_repo(request: Request) -> ApprovalRepository:
    repo = getattr(request.app.state, "approval_repository", None)
    if repo is None:
        raise ApiServiceError(
            "APPROVAL_STORE_UNAVAILABLE",
            "approval persistence is not configured",
            503,
        )
    return repo


def _tool_effect_repo(request: Request) -> ToolEffectRepository:
    repo = getattr(request.app.state, "tool_effect_repository", None)
    if repo is None:
        raise ApiServiceError(
            "TOOL_EFFECT_STORE_UNAVAILABLE",
            "tool effect persistence is not configured",
            503,
        )
    return repo


def _approval_service(request: Request) -> ApprovalService:
    service = getattr(request.app.state, "approval_service", None)
    if service is None:
        raise ApiServiceError(
            "APPROVAL_SERVICE_UNAVAILABLE",
            "approval service is not configured",
            503,
        )
    return service


def _plan_view(plan: OperationPlan) -> dict[str, object]:
    return {
        "id": plan.plan_id,
        "task_id": plan.task_id,
        "environment_id": plan.environment_id,
        "action_type": plan.action_type,
        "resource_kind": plan.resource_kind,
        "resource_name": plan.resource_name,
        "namespace": plan.namespace,
        "risk_level": plan.risk_level,
        "required_scope": plan.required_scope,
        "plan_hash": plan.plan_hash,
        "canonical": plan.canonical,
        "parameters": plan.parameters,
        "preconditions": plan.preconditions,
        "postconditions": plan.postconditions,
        "rollback": plan.rollback,
        "dry_run": plan.dry_run,
        "status": plan.status,
        "created_by": plan.created_by,
        "created_at": plan.created_at,
        "expires_at": plan.expires_at,
    }


def _approval_view(approval: Approval) -> dict[str, object]:
    return {
        "id": approval.approval_id,
        "plan_id": approval.plan_id,
        "plan_hash": approval.plan_hash,
        "status": approval.status,
        "requested_by": approval.requested_by,
        "requested_at": approval.requested_at,
        "decided_by": approval.decided_by,
        "decided_at": approval.decided_at,
        "decision_note": approval.decision_note,
        "scopes": approval.scopes,
        "expires_at": approval.expires_at,
    }


def _k8s_action_plan(plan: OperationPlan) -> K8sActionPlan:
    return K8sActionPlan(
        action_type=plan.action_type,
        namespace=plan.namespace,
        resource_kind=plan.resource_kind,
        resource_name=plan.resource_name,
        risk="low" if plan.risk_level == "S3" else plan.risk_level.lower(),
        command_preview=str(plan.dry_run.get("command_preview") or ""),
        parameters=plan.parameters,
        environment=str(plan.canonical.get("environment", "dev")),
        actor=plan.created_by,
        required_scope=plan.required_scope,
        rollback_suggestion=str(plan.rollback.get("command") or ""),
        security={"required_scope": plan.required_scope, "plan_hash": plan.plan_hash},
    )


def _service_error(exc: ApprovalServiceError) -> ApiServiceError:
    statuses = {
        "PLAN_NOT_FOUND": 404,
        "APPROVAL_NOT_FOUND": 404,
        "PLAN_HASH_MISMATCH": 409,
        "APPROVAL_NOT_PENDING": 409,
        "PLAN_NOT_APPROVABLE": 409,
        "PLAN_EXPIRED": 409,
        "APPROVAL_EXPIRED": 409,
        "APPROVAL_SCOPE_DENIED": 403,
        "PLAN_RISK_LEVEL_REJECTED": 403,
        "PLAN_RISK_LEVEL_UNSUPPORTED": 400,
        "PLAN_DRY_RUN_REQUIRED": 400,
    }
    return ApiServiceError(
        exc.error_code,
        exc.error_code.lower().replace("_", " "),
        status_code=statuses.get(exc.error_code, 400),
    )


@plans_router.post("", status_code=status.HTTP_201_CREATED)
async def create_plan(
    payload: OperationPlanCreate,
    request: Request,
    tenant: TenantContext = Depends(require_scope("plan:create")),
) -> ApiResponse[dict[str, object]]:
    try:
        plan, replayed = await _approval_service(request).create_plan(
            tenant,
            task_id=payload.task_id,
            environment_id=payload.environment_id,
            action_type=payload.action_type,
            resource_kind=payload.resource_kind,
            resource_name=payload.resource_name,
            namespace=payload.namespace,
            risk_level=payload.risk_level,
            required_scope=payload.required_scope,
            parameters=payload.parameters,
            preconditions=payload.preconditions,
            postconditions=payload.postconditions,
            rollback=payload.rollback,
            dry_run=payload.dry_run,
            expires_in_seconds=payload.expires_in_seconds,
        )
    except ApprovalServiceError as exc:
        raise _service_error(exc) from exc
    result = _plan_view(plan)
    result["replayed"] = replayed
    return ApiResponse.ok(result)


@plans_router.get("")
async def list_plans(
    request: Request,
    plan_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=200),
    tenant: TenantContext = Depends(require_scope("plan:read")),
) -> ApiResponse[dict[str, object]]:
    items = await _plan_repo(request).list(
        tenant.tenant_id, status=plan_status, limit=limit
    )
    return ApiResponse.ok({"items": [_plan_view(item) for item in items]})


@plans_router.get("/{plan_id}")
async def get_plan(
    plan_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("plan:read")),
) -> ApiResponse[dict[str, object]]:
    plan = await _plan_repo(request).get(tenant.tenant_id, plan_id)
    if plan is None:
        raise ApiServiceError("PLAN_NOT_FOUND", "operation plan not found", 404)
    return ApiResponse.ok(_plan_view(plan))


@plans_router.post("/{plan_id}/request-approval")
async def request_approval(
    plan_id: str,
    request: Request,
    tenant: TenantContext = Depends(require_scope("plan:request")),
) -> ApiResponse[dict[str, object]]:
    try:
        approval = await _approval_service(request).request_approval(tenant, plan_id)
    except ApprovalServiceError as exc:
        raise _service_error(exc) from exc
    return ApiResponse.ok(_approval_view(approval))


@plans_router.post("/{plan_id}/execute")
async def execute_plan(
    plan_id: str,
    payload: OperationPlanExecute,
    request: Request,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    tenant: TenantContext = Depends(require_scope("cloud:execute")),
) -> ApiResponse[dict[str, object]]:
    if not idempotency_key:
        raise ApiServiceError(
            "IDEMPOTENCY_KEY_REQUIRED",
            "Idempotency-Key header is required",
            status_code=400,
        )
    settings = getattr(request.app.state, "settings", None)
    ops_security = settings.ops.security
    if ops_security.default_readonly:
        raise ApiServiceError(
            "WRITE_EXECUTION_DISABLED",
            "controlled write execution is disabled by default",
            status_code=403,
        )
    plan = await _plan_repo(request).get(tenant.tenant_id, plan_id)
    if plan is None:
        raise ApiServiceError("PLAN_NOT_FOUND", "operation plan not found", 404)
    if payload.plan_hash != plan.plan_hash:
        raise ApiServiceError("PLAN_HASH_MISMATCH", "plan hash mismatch", 409)
    approval = await _approval_repo(request).get(tenant.tenant_id, payload.approval_id)
    if (
        approval is None
        or approval.plan_id != plan.plan_id
        or approval.plan_hash != plan.plan_hash
        or approval.status != "approved"
    ):
        raise ApiServiceError("APPROVAL_REQUIRED", "approved approval is required", 409)
    if not tenant.has_scope(plan.required_scope):
        raise ApiServiceError(
            "PLAN_EXECUTION_SCOPE_DENIED",
            "tenant does not have the plan execution scope",
            403,
        )

    effects = _tool_effect_repo(request)
    task_id = plan.task_id or plan.plan_id
    if plan.status != "approved":
        existing = await effects.get(
            tenant_id=tenant.tenant_id,
            task_id=task_id,
            call_id=idempotency_key,
        )
        if existing is not None and existing.status in {"succeeded", "failed"}:
            return ApiResponse.ok(
                {
                    "replayed": True,
                    "effect": existing.__dict__,
                    "result": existing.result,
                    "post_condition": existing.post_condition,
                }
            )
        raise ApiServiceError(
            "PLAN_NOT_APPROVED", "operation plan is not approved", 409
        )

    arguments = {
        "plan_id": plan.plan_id,
        "plan_hash": plan.plan_hash,
        "action_type": plan.action_type,
        "namespace": plan.namespace,
        "resource_kind": plan.resource_kind,
        "resource_name": plan.resource_name,
        "parameters": plan.parameters,
    }
    try:
        effect, replayed = await effects.start(
            tenant_id=tenant.tenant_id,
            task_id=task_id,
            call_id=idempotency_key,
            tool_name=f"k8s.{plan.action_type}",
            arguments=arguments,
            plan_hash=plan.plan_hash,
        )
    except ToolEffectConflictError as exc:
        raise ApiServiceError(
            "TOOL_CALL_ID_REUSED",
            "Idempotency-Key was already used for a different write request",
            409,
        ) from exc
    if replayed:
        if effect.status == "started":
            raise ApiServiceError(
                "TOOL_EFFECT_IN_PROGRESS",
                "write operation is already in progress",
                409,
            )
        return ApiResponse.ok(
            {
                "replayed": True,
                "effect": effect.__dict__,
                "result": effect.result,
                "post_condition": effect.post_condition,
            }
        )

    executor = K8sWriteActionExecutor(
        request.app.state.ops_k8s_client,
        K8sActionSecurityPolicy.from_settings(ops_security),
        actor=tenant.tenant_id,
        required_scope=plan.required_scope,
    )
    result = executor.execute(_k8s_action_plan(plan))
    finished = await effects.finish(
        tenant_id=tenant.tenant_id,
        task_id=task_id,
        call_id=idempotency_key,
        result=result.to_dict(),
        post_condition=result.verification,
        error_code=None if result.success else "K8S_ACTION_FAILED",
    )
    status_value = "executed" if result.success else "failed"
    updated_plan = await _plan_repo(request).set_status(
        tenant.tenant_id, plan.plan_id, status_value
    )
    assert updated_plan is not None
    return ApiResponse.ok(
        {
            "replayed": False,
            "effect": finished.__dict__,
            "plan": _plan_view(updated_plan),
            "result": result.to_dict(),
            "post_condition": result.verification,
        }
    )


@approvals_router.get("")
async def list_approvals(
    request: Request,
    approval_status: str | None = Query(default=None, alias="status"),
    plan_id: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=200),
    tenant: TenantContext = Depends(require_scope("approval:read")),
) -> ApiResponse[dict[str, object]]:
    items = await _approval_repo(request).list(
        tenant.tenant_id, status=approval_status, plan_id=plan_id, limit=limit
    )
    return ApiResponse.ok({"items": [_approval_view(item) for item in items]})


@approvals_router.post("/{approval_id}/approve")
async def approve(
    approval_id: str,
    payload: ApprovalDecision,
    request: Request,
    tenant: TenantContext = Depends(require_scope("approval:approve")),
) -> ApiResponse[dict[str, object]]:
    try:
        approval, plan = await _approval_service(request).approve(
            tenant,
            approval_id,
            plan_hash=payload.plan_hash,
            note=payload.note,
        )
    except ApprovalServiceError as exc:
        raise _service_error(exc) from exc
    return ApiResponse.ok(
        {"approval": _approval_view(approval), "plan": _plan_view(plan)}
    )


@approvals_router.post("/{approval_id}/reject")
async def reject(
    approval_id: str,
    payload: ApprovalReject,
    request: Request,
    tenant: TenantContext = Depends(require_scope("approval:approve")),
) -> ApiResponse[dict[str, object]]:
    try:
        approval, plan = await _approval_service(request).reject(
            tenant, approval_id, note=payload.note
        )
    except ApprovalServiceError as exc:
        raise _service_error(exc) from exc
    return ApiResponse.ok(
        {"approval": _approval_view(approval), "plan": _plan_view(plan)}
    )
