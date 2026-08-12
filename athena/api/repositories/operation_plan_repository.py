"""Tenant-scoped immutable OperationPlan and Approval persistence."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import ApprovalModel, OperationPlanModel


def _canonical_json(payload: dict[str, object]) -> str:
    return json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def canonical_plan_hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class OperationPlan:
    plan_id: str
    tenant_id: str
    task_id: str | None
    environment_id: str
    action_type: str
    resource_kind: str
    resource_name: str
    namespace: str
    risk_level: str
    required_scope: str
    plan_hash: str
    canonical: dict[str, object]
    parameters: dict[str, object]
    preconditions: dict[str, object]
    postconditions: dict[str, object]
    rollback: dict[str, object]
    dry_run: dict[str, object]
    status: str
    created_by: str
    created_at: datetime
    expires_at: datetime | None


@dataclass(frozen=True)
class Approval:
    approval_id: str
    tenant_id: str
    plan_id: str
    plan_hash: str
    status: str
    requested_by: str
    requested_at: datetime
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    scopes: tuple[str, ...]
    expires_at: datetime | None


class OperationPlanConflictError(RuntimeError):
    pass


class OperationPlanStateError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        self.error_code = error_code
        super().__init__(error_code)


class OperationPlanRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_immutable(
        self,
        tenant_id: str,
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
        created_by: str,
        expires_in_seconds: int | None = 3600,
    ) -> tuple[OperationPlan, bool]:
        canonical = {
            "action_type": action_type,
            "dry_run": dict(dry_run),
            "environment_id": environment_id,
            "namespace": namespace,
            "parameters": dict(parameters),
            "postconditions": dict(postconditions),
            "preconditions": dict(preconditions),
            "required_scope": required_scope,
            "resource_kind": resource_kind,
            "resource_name": resource_name,
            "risk_level": risk_level,
            "rollback": dict(rollback),
            "task_id": task_id,
        }
        plan_hash = canonical_plan_hash(canonical)
        now = datetime.now(UTC)
        expires_at = (
            now + timedelta(seconds=expires_in_seconds) if expires_in_seconds else None
        )
        async with self._sessions() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(OperationPlanModel).where(
                        OperationPlanModel.tenant_id == tenant_id,
                        OperationPlanModel.plan_hash == plan_hash,
                    )
                )
                if existing is not None:
                    return self._from_plan(existing), True
                model = OperationPlanModel(
                    id=f"plan-{uuid.uuid4().hex}",
                    tenant_id=tenant_id,
                    task_id=task_id,
                    environment_id=environment_id,
                    action_type=action_type,
                    resource_kind=resource_kind,
                    resource_name=resource_name,
                    namespace=namespace,
                    risk_level=risk_level,
                    required_scope=required_scope,
                    plan_hash=plan_hash,
                    canonical_json=canonical,
                    parameters_json=dict(parameters),
                    preconditions_json=dict(preconditions),
                    postconditions_json=dict(postconditions),
                    rollback_json=dict(rollback),
                    dry_run_json=dict(dry_run),
                    status="draft",
                    created_by=created_by,
                    created_at=now,
                    expires_at=expires_at,
                )
                session.add(model)
                return self._from_plan(model), False

    async def get(self, tenant_id: str, plan_id: str) -> OperationPlan | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(OperationPlanModel).where(
                    OperationPlanModel.tenant_id == tenant_id,
                    OperationPlanModel.id == plan_id,
                )
            )
        return self._from_plan(model) if model is not None else None

    async def list(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        limit: int = 100,
    ) -> tuple[OperationPlan, ...]:
        stmt = select(OperationPlanModel).where(
            OperationPlanModel.tenant_id == tenant_id
        )
        if status:
            stmt = stmt.where(OperationPlanModel.status == status)
        stmt = stmt.order_by(OperationPlanModel.created_at.desc()).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(stmt)).all()
        return tuple(self._from_plan(row) for row in rows)

    async def set_status(
        self, tenant_id: str, plan_id: str, status: str
    ) -> OperationPlan | None:
        async with self._sessions() as session:
            async with session.begin():
                model = await self._locked_plan(session, tenant_id, plan_id)
                if model is None:
                    return None
                model.status = status
        return self._from_plan(model)

    async def _locked_plan(
        self, session: AsyncSession, tenant_id: str, plan_id: str
    ) -> OperationPlanModel | None:
        return await session.scalar(
            select(OperationPlanModel)
            .where(
                OperationPlanModel.tenant_id == tenant_id,
                OperationPlanModel.id == plan_id,
            )
            .with_for_update()
        )

    @staticmethod
    def _from_plan(model: OperationPlanModel) -> OperationPlan:
        return OperationPlan(
            plan_id=model.id,
            tenant_id=model.tenant_id,
            task_id=model.task_id,
            environment_id=model.environment_id,
            action_type=model.action_type,
            resource_kind=model.resource_kind,
            resource_name=model.resource_name,
            namespace=model.namespace,
            risk_level=model.risk_level,
            required_scope=model.required_scope,
            plan_hash=model.plan_hash,
            canonical=dict(model.canonical_json or {}),
            parameters=dict(model.parameters_json or {}),
            preconditions=dict(model.preconditions_json or {}),
            postconditions=dict(model.postconditions_json or {}),
            rollback=dict(model.rollback_json or {}),
            dry_run=dict(model.dry_run_json or {}),
            status=model.status,
            created_by=model.created_by,
            created_at=model.created_at,
            expires_at=model.expires_at,
        )


class ApprovalRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def request(
        self,
        tenant_id: str,
        *,
        plan_id: str,
        plan_hash: str,
        requested_by: str,
        scopes: tuple[str, ...],
        expires_at: datetime | None,
    ) -> Approval:
        async with self._sessions() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(ApprovalModel).where(
                        ApprovalModel.tenant_id == tenant_id,
                        ApprovalModel.plan_id == plan_id,
                        ApprovalModel.status == "pending",
                    )
                )
                if existing is not None:
                    return self._from_approval(existing)
                model = ApprovalModel(
                    id=f"approval-{uuid.uuid4().hex}",
                    tenant_id=tenant_id,
                    plan_id=plan_id,
                    plan_hash=plan_hash,
                    status="pending",
                    requested_by=requested_by,
                    requested_at=datetime.now(UTC),
                    scopes_json=list(scopes),
                    expires_at=expires_at,
                )
                session.add(model)
                return self._from_approval(model)

    async def get(self, tenant_id: str, approval_id: str) -> Approval | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(ApprovalModel).where(
                    ApprovalModel.tenant_id == tenant_id,
                    ApprovalModel.id == approval_id,
                )
            )
        return self._from_approval(model) if model is not None else None

    async def list(
        self,
        tenant_id: str,
        *,
        status: str | None = None,
        plan_id: str | None = None,
        limit: int = 100,
    ) -> tuple[Approval, ...]:
        stmt = select(ApprovalModel).where(ApprovalModel.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(ApprovalModel.status == status)
        if plan_id:
            stmt = stmt.where(ApprovalModel.plan_id == plan_id)
        stmt = stmt.order_by(ApprovalModel.requested_at.desc()).limit(limit)
        async with self._sessions() as session:
            rows = (await session.scalars(stmt)).all()
        return tuple(self._from_approval(row) for row in rows)

    async def decide(
        self,
        tenant_id: str,
        approval_id: str,
        *,
        status: str,
        decided_by: str,
        note: str | None,
    ) -> Approval | None:
        async with self._sessions() as session:
            async with session.begin():
                model = await session.scalar(
                    select(ApprovalModel)
                    .where(
                        ApprovalModel.tenant_id == tenant_id,
                        ApprovalModel.id == approval_id,
                    )
                    .with_for_update()
                )
                if model is None:
                    return None
                if model.status != "pending":
                    raise OperationPlanStateError("APPROVAL_NOT_PENDING")
                if _expired(model.expires_at):
                    model.status = "expired"
                    raise OperationPlanStateError("APPROVAL_EXPIRED")
                model.status = status
                model.decided_by = decided_by
                model.decided_at = datetime.now(UTC)
                model.decision_note = note
        return self._from_approval(model)

    @staticmethod
    def _from_approval(model: ApprovalModel) -> Approval:
        return Approval(
            approval_id=model.id,
            tenant_id=model.tenant_id,
            plan_id=model.plan_id,
            plan_hash=model.plan_hash,
            status=model.status,
            requested_by=model.requested_by,
            requested_at=model.requested_at,
            decided_by=model.decided_by,
            decided_at=model.decided_at,
            decision_note=model.decision_note,
            scopes=tuple(model.scopes_json or ()),
            expires_at=model.expires_at,
        )


def _expired(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value < datetime.now(UTC)
