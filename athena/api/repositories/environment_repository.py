"""Tenant-scoped persistence for CloudOps connection declarations."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import EnvironmentModel


@dataclass(frozen=True)
class PersistedEnvironment:
    environment_id: str
    tenant_id: str
    name: str
    environment_type: str
    provider: str
    mode: str
    scope: dict[str, object]
    credential_ref: str | None
    capabilities: tuple[str, ...]
    status: str
    last_checked_at: datetime | None


class EnvironmentRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create(
        self,
        tenant_id: str,
        *,
        name: str,
        environment_type: str,
        provider: str,
        mode: str,
        scope: dict[str, object],
        credential_ref: str | None,
        capabilities: tuple[str, ...],
    ) -> PersistedEnvironment:
        model = EnvironmentModel(
            id=f"env-{uuid.uuid4().hex}",
            tenant_id=tenant_id,
            name=name,
            environment_type=environment_type,
            provider=provider,
            mode=mode,
            scope_json=dict(scope),
            credential_ref=credential_ref,
            capabilities_json=list(capabilities),
        )
        async with self._sessions() as session:
            async with session.begin():
                session.add(model)
        return self._from(model)

    async def get(
        self, tenant_id: str, environment_id: str
    ) -> PersistedEnvironment | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(EnvironmentModel).where(
                    EnvironmentModel.id == environment_id,
                    EnvironmentModel.tenant_id == tenant_id,
                )
            )
        return self._from(model) if model is not None else None

    async def list(self, tenant_id: str) -> tuple[PersistedEnvironment, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(EnvironmentModel)
                    .where(EnvironmentModel.tenant_id == tenant_id)
                    .order_by(EnvironmentModel.created_at.desc())
                )
            ).all()
        return tuple(self._from(row) for row in rows)

    async def update(
        self,
        tenant_id: str,
        environment_id: str,
        *,
        name: str | None = None,
        scope: dict[str, object] | None = None,
        credential_ref: str | None = None,
    ) -> PersistedEnvironment | None:
        async with self._sessions() as session:
            async with session.begin():
                model = await self._locked(session, tenant_id, environment_id)
                if model is None:
                    return None
                if name is not None:
                    model.name = name
                if scope is not None:
                    model.scope_json = dict(scope)
                if credential_ref is not None:
                    model.credential_ref = credential_ref
        return self._from(model)

    async def delete(self, tenant_id: str, environment_id: str) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                model = await self._locked(session, tenant_id, environment_id)
                if model is None:
                    return False
                await session.delete(model)
        return True

    async def set_status(
        self, tenant_id: str, environment_id: str, status: str
    ) -> PersistedEnvironment | None:
        async with self._sessions() as session:
            async with session.begin():
                model = await self._locked(session, tenant_id, environment_id)
                if model is None:
                    return None
                model.status = status
                model.last_checked_at = datetime.now(UTC)
        return self._from(model)

    @staticmethod
    async def _locked(
        session: AsyncSession, tenant_id: str, environment_id: str
    ) -> EnvironmentModel | None:
        return await session.scalar(
            select(EnvironmentModel)
            .where(
                EnvironmentModel.id == environment_id,
                EnvironmentModel.tenant_id == tenant_id,
            )
            .with_for_update()
        )

    @staticmethod
    def _from(model: EnvironmentModel) -> PersistedEnvironment:
        return PersistedEnvironment(
            environment_id=model.id,
            tenant_id=model.tenant_id,
            name=model.name,
            environment_type=model.environment_type,
            provider=model.provider,
            mode=model.mode,
            scope=dict(model.scope_json or {}),
            credential_ref=model.credential_ref,
            capabilities=tuple(model.capabilities_json or ()),
            status=model.status,
            last_checked_at=model.last_checked_at,
        )
