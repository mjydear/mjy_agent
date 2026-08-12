"""Idempotent durable records for external Tool side effects."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import ToolEffectModel


class ToolEffectConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class ToolEffect:
    effect_id: str
    tenant_id: str
    task_id: str
    call_id: str
    tool_name: str
    plan_hash: str | None
    status: str
    result: dict[str, object] | None
    post_condition: dict[str, object] | None
    error_code: str | None


class ToolEffectRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def start(
        self,
        *,
        tenant_id: str,
        task_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, object],
        plan_hash: str | None,
    ) -> tuple[ToolEffect, bool]:
        request_hash = self._hash(arguments)
        async with self._sessions() as session:
            async with session.begin():
                existing = await session.scalar(
                    select(ToolEffectModel)
                    .where(
                        ToolEffectModel.tenant_id == tenant_id,
                        ToolEffectModel.task_id == task_id,
                        ToolEffectModel.call_id == call_id,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if (
                        existing.request_hash != request_hash
                        or existing.tool_name != tool_name
                    ):
                        raise ToolEffectConflictError("TOOL_CALL_ID_REUSED")
                    return self._from_model(existing), True
                effect = ToolEffectModel(
                    id=f"effect-{uuid.uuid4().hex}",
                    tenant_id=tenant_id,
                    task_id=task_id,
                    call_id=call_id,
                    tool_name=tool_name,
                    plan_hash=plan_hash,
                    request_hash=request_hash,
                    status="started",
                )
                session.add(effect)
                return self._from_model(effect), False

    async def get(
        self, *, tenant_id: str, task_id: str, call_id: str
    ) -> ToolEffect | None:
        async with self._sessions() as session:
            effect = await session.scalar(
                select(ToolEffectModel).where(
                    ToolEffectModel.tenant_id == tenant_id,
                    ToolEffectModel.task_id == task_id,
                    ToolEffectModel.call_id == call_id,
                )
            )
        return self._from_model(effect) if effect is not None else None

    async def finish(
        self,
        *,
        tenant_id: str,
        task_id: str,
        call_id: str,
        result: dict[str, object] | None,
        post_condition: dict[str, object] | None,
        error_code: str | None = None,
    ) -> ToolEffect:
        async with self._sessions() as session:
            async with session.begin():
                effect = await session.scalar(
                    select(ToolEffectModel)
                    .where(
                        ToolEffectModel.tenant_id == tenant_id,
                        ToolEffectModel.task_id == task_id,
                        ToolEffectModel.call_id == call_id,
                    )
                    .with_for_update()
                )
                if effect is None:
                    raise KeyError("TOOL_EFFECT_NOT_FOUND")
                if effect.status in {"succeeded", "failed"}:
                    return self._from_model(effect)
                effect.result_json = result
                effect.post_condition_json = post_condition
                effect.error_code = error_code
                effect.status = "failed" if error_code else "succeeded"
                effect.finished_at = datetime.now(UTC)
                return self._from_model(effect)

    @staticmethod
    def _hash(arguments: dict[str, object]) -> str:
        serialized = json.dumps(
            arguments,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _from_model(effect: ToolEffectModel) -> ToolEffect:
        return ToolEffect(
            effect_id=effect.id,
            tenant_id=effect.tenant_id,
            task_id=effect.task_id,
            call_id=effect.call_id,
            tool_name=effect.tool_name,
            plan_hash=effect.plan_hash,
            status=effect.status,
            result=dict(effect.result_json) if effect.result_json else None,
            post_condition=(
                dict(effect.post_condition_json) if effect.post_condition_json else None
            ),
            error_code=effect.error_code,
        )
