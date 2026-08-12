"""OperationPlan approval lifecycle and guardrails."""

from __future__ import annotations

from datetime import UTC, datetime

from athena.api.auth import TenantContext
from athena.api.repositories.operation_plan_repository import (
    Approval,
    ApprovalRepository,
    OperationPlan,
    OperationPlanRepository,
    OperationPlanStateError,
)


class ApprovalServiceError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class ApprovalService:
    def __init__(
        self,
        plans: OperationPlanRepository,
        approvals: ApprovalRepository,
    ) -> None:
        self._plans = plans
        self._approvals = approvals

    async def create_plan(
        self,
        tenant: TenantContext,
        *,
        task_id: str | None,
        environment_id: str,
        action_type: str,
        resource_kind: str,
        resource_name: str,
        namespace: str,
        risk_level: str,
        required_scope: str,
        parameters: dict[str, object],
        preconditions: dict[str, object],
        postconditions: dict[str, object],
        rollback: dict[str, object],
        dry_run: dict[str, object],
        expires_in_seconds: int | None,
    ) -> tuple[OperationPlan, bool]:
        if risk_level not in {"S3", "S4"}:
            raise ApprovalServiceError("PLAN_RISK_LEVEL_UNSUPPORTED")
        if risk_level == "S4":
            raise ApprovalServiceError("PLAN_RISK_LEVEL_REJECTED")
        if not dry_run:
            raise ApprovalServiceError("PLAN_DRY_RUN_REQUIRED")
        return await self._plans.create_immutable(
            tenant.tenant_id,
            task_id=task_id,
            environment_id=environment_id,
            action_type=action_type,
            resource_kind=resource_kind,
            resource_name=resource_name,
            namespace=namespace,
            risk_level=risk_level,
            required_scope=required_scope,
            parameters=parameters,
            preconditions=preconditions,
            postconditions=postconditions,
            rollback=rollback,
            dry_run=dry_run,
            created_by=tenant.api_key or tenant.tenant_id,
            expires_in_seconds=expires_in_seconds,
        )

    async def request_approval(self, tenant: TenantContext, plan_id: str) -> Approval:
        plan = await self._plans.get(tenant.tenant_id, plan_id)
        if plan is None:
            raise ApprovalServiceError("PLAN_NOT_FOUND")
        if plan.status not in {"draft", "approval_pending"}:
            raise ApprovalServiceError("PLAN_NOT_APPROVABLE")
        if _expired(plan.expires_at):
            await self._plans.set_status(tenant.tenant_id, plan_id, "expired")
            raise ApprovalServiceError("PLAN_EXPIRED")
        approval = await self._approvals.request(
            tenant.tenant_id,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            requested_by=tenant.api_key or tenant.tenant_id,
            scopes=(plan.required_scope,),
            expires_at=plan.expires_at,
        )
        await self._plans.set_status(tenant.tenant_id, plan_id, "approval_pending")
        return approval

    async def approve(
        self,
        tenant: TenantContext,
        approval_id: str,
        *,
        plan_hash: str,
        note: str | None,
    ) -> tuple[Approval, OperationPlan]:
        approval = await self._approvals.get(tenant.tenant_id, approval_id)
        if approval is None:
            raise ApprovalServiceError("APPROVAL_NOT_FOUND")
        plan = await self._plans.get(tenant.tenant_id, approval.plan_id)
        if plan is None:
            raise ApprovalServiceError("PLAN_NOT_FOUND")
        if plan_hash != approval.plan_hash or plan_hash != plan.plan_hash:
            raise ApprovalServiceError("PLAN_HASH_MISMATCH")
        if _expired(plan.expires_at):
            await self._plans.set_status(tenant.tenant_id, plan.plan_id, "expired")
            raise ApprovalServiceError("PLAN_EXPIRED")
        if not tenant.has_scope(plan.required_scope):
            raise ApprovalServiceError("APPROVAL_SCOPE_DENIED")
        try:
            decided = await self._approvals.decide(
                tenant.tenant_id,
                approval_id,
                status="approved",
                decided_by=tenant.api_key or tenant.tenant_id,
                note=note,
            )
        except OperationPlanStateError as exc:
            raise ApprovalServiceError(exc.error_code) from exc
        if decided is None:
            raise ApprovalServiceError("APPROVAL_NOT_FOUND")
        updated = await self._plans.set_status(
            tenant.tenant_id, plan.plan_id, "approved"
        )
        assert updated is not None
        return decided, updated

    async def reject(
        self,
        tenant: TenantContext,
        approval_id: str,
        *,
        note: str | None,
    ) -> tuple[Approval, OperationPlan]:
        approval = await self._approvals.get(tenant.tenant_id, approval_id)
        if approval is None:
            raise ApprovalServiceError("APPROVAL_NOT_FOUND")
        plan = await self._plans.get(tenant.tenant_id, approval.plan_id)
        if plan is None:
            raise ApprovalServiceError("PLAN_NOT_FOUND")
        try:
            decided = await self._approvals.decide(
                tenant.tenant_id,
                approval_id,
                status="rejected",
                decided_by=tenant.api_key or tenant.tenant_id,
                note=note,
            )
        except OperationPlanStateError as exc:
            raise ApprovalServiceError(exc.error_code) from exc
        if decided is None:
            raise ApprovalServiceError("APPROVAL_NOT_FOUND")
        updated = await self._plans.set_status(
            tenant.tenant_id, plan.plan_id, "rejected"
        )
        assert updated is not None
        return decided, updated


def _expired(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value < datetime.now(UTC)
