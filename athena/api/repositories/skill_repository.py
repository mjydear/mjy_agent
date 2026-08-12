"""Tenant-scoped Skill definition/version lifecycle repository."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import SkillDefinitionModel, SkillVersionModel

ACTIVE_STATUS = "active"
ARCHIVED_STATUS = "archived"
DRAFT_STATUS = "draft"
EVALUATING_STATUS = "evaluating"
REJECTED_STATUS = "rejected"
REVIEW_PENDING_STATUS = "review_pending"


class SkillLifecycleError(RuntimeError):
    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


@dataclass(frozen=True)
class SkillDefinition:
    skill_id: str
    tenant_id: str
    name: str
    owner: str
    environment_type: str
    capabilities: tuple[str, ...]
    active_version_id: str | None


@dataclass(frozen=True)
class SkillVersion:
    version_id: str
    tenant_id: str
    skill_id: str
    version: int
    status: str
    manifest: dict[str, object]
    procedure: dict[str, object]
    checksum: str
    source_task_id: str | None
    benchmark_report_id: str | None
    created_by: str
    reviewed_by: str | None
    review_note: str | None


class SkillRepository:
    """Persist immutable Skill versions with an atomic active pointer."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def create_draft(
        self,
        tenant_id: str,
        *,
        name: str,
        owner: str,
        environment_type: str,
        capabilities: tuple[str, ...],
        manifest: dict[str, object],
        procedure: dict[str, object],
        created_by: str,
        source_task_id: str | None = None,
        benchmark_report_id: str | None = None,
    ) -> tuple[SkillDefinition, SkillVersion]:
        self._validate_manifest(manifest, capabilities)
        checksum = self._checksum(manifest, procedure)
        async with self._sessions() as session:
            async with session.begin():
                definition = await session.scalar(
                    select(SkillDefinitionModel)
                    .where(
                        SkillDefinitionModel.tenant_id == tenant_id,
                        SkillDefinitionModel.name == name,
                    )
                    .with_for_update()
                )
                if definition is None:
                    definition = SkillDefinitionModel(
                        id=f"skill-{uuid.uuid4().hex}",
                        tenant_id=tenant_id,
                        name=name,
                        owner=owner,
                        environment_type=environment_type,
                        capabilities_json=list(capabilities),
                    )
                    session.add(definition)
                    version_number = 1
                else:
                    version_number = (
                        int(
                            await session.scalar(
                                select(
                                    func.coalesce(
                                        func.max(SkillVersionModel.version), 0
                                    )
                                ).where(
                                    SkillVersionModel.tenant_id == tenant_id,
                                    SkillVersionModel.skill_id == definition.id,
                                )
                            )
                        )
                        + 1
                    )
                    definition.capabilities_json = list(capabilities)
                    definition.environment_type = environment_type
                version = SkillVersionModel(
                    id=f"skill-version-{uuid.uuid4().hex}",
                    tenant_id=tenant_id,
                    skill_id=definition.id,
                    version=version_number,
                    status=DRAFT_STATUS,
                    manifest_json=dict(manifest),
                    procedure_json=dict(procedure),
                    checksum=checksum,
                    source_task_id=source_task_id,
                    benchmark_report_id=benchmark_report_id,
                    created_by=created_by,
                )
                session.add(version)
        return self._definition_from(definition), self._version_from(version)

    async def submit_review(
        self, tenant_id: str, version_id: str
    ) -> SkillVersion | None:
        async with self._sessions() as session:
            async with session.begin():
                version = await self._locked_version(session, tenant_id, version_id)
                if version is None:
                    return None
                if version.status not in {DRAFT_STATUS, EVALUATING_STATUS}:
                    raise SkillLifecycleError("SKILL_VERSION_NOT_REVIEWABLE")
                version.status = REVIEW_PENDING_STATUS
        return self._version_from(version)

    async def record_evaluation(
        self,
        tenant_id: str,
        version_id: str,
        *,
        report_id: str,
        passed: bool,
    ) -> SkillVersion | None:
        async with self._sessions() as session:
            async with session.begin():
                version = await self._locked_version(session, tenant_id, version_id)
                if version is None:
                    return None
                if version.status not in {DRAFT_STATUS, EVALUATING_STATUS}:
                    raise SkillLifecycleError("SKILL_VERSION_NOT_EVALUATABLE")
                version.benchmark_report_id = report_id
                version.status = REVIEW_PENDING_STATUS if passed else EVALUATING_STATUS
        return self._version_from(version)

    async def reject(
        self, tenant_id: str, version_id: str, *, reviewed_by: str, note: str
    ) -> SkillVersion | None:
        async with self._sessions() as session:
            async with session.begin():
                version = await self._locked_version(session, tenant_id, version_id)
                if version is None:
                    return None
                if version.status != REVIEW_PENDING_STATUS:
                    raise SkillLifecycleError("SKILL_VERSION_NOT_PENDING_REVIEW")
                version.status = REJECTED_STATUS
                version.reviewed_by = reviewed_by
                version.review_note = note
                version.decided_at = datetime.now(UTC)
        return self._version_from(version)

    async def approve(
        self, tenant_id: str, version_id: str, *, reviewed_by: str, note: str = ""
    ) -> SkillVersion | None:
        async with self._sessions() as session:
            async with session.begin():
                version = await self._locked_version(session, tenant_id, version_id)
                if version is None:
                    return None
                if version.status != REVIEW_PENDING_STATUS:
                    raise SkillLifecycleError("SKILL_VERSION_NOT_PENDING_REVIEW")
                await self._activate(
                    session, version, reviewed_by=reviewed_by, note=note
                )
        return self._version_from(version)

    async def rollback(
        self,
        tenant_id: str,
        *,
        skill_id: str,
        target_version_id: str,
        reviewed_by: str,
        note: str = "rollback",
    ) -> SkillVersion | None:
        async with self._sessions() as session:
            async with session.begin():
                version = await self._locked_version(
                    session, tenant_id, target_version_id
                )
                if version is None or version.skill_id != skill_id:
                    return None
                if version.status not in {ACTIVE_STATUS, ARCHIVED_STATUS}:
                    raise SkillLifecycleError("SKILL_ROLLBACK_TARGET_NOT_ACTIVATABLE")
                await self._activate(
                    session, version, reviewed_by=reviewed_by, note=note
                )
        return self._version_from(version)

    async def get_active(self, tenant_id: str, skill_id: str) -> SkillVersion | None:
        async with self._sessions() as session:
            definition = await session.scalar(
                select(SkillDefinitionModel).where(
                    SkillDefinitionModel.tenant_id == tenant_id,
                    SkillDefinitionModel.id == skill_id,
                )
            )
            if definition is None or not definition.active_version_id:
                return None
            version = await self._version(
                session, tenant_id, definition.active_version_id
            )
        return self._version_from(version) if version is not None else None

    async def list_active_for_capabilities(
        self,
        tenant_id: str,
        *,
        environment_type: str,
        capabilities: frozenset[str],
    ) -> tuple[SkillVersion, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(SkillVersionModel)
                    .join(
                        SkillDefinitionModel,
                        SkillDefinitionModel.id == SkillVersionModel.skill_id,
                    )
                    .where(
                        SkillVersionModel.tenant_id == tenant_id,
                        SkillVersionModel.status == ACTIVE_STATUS,
                        SkillDefinitionModel.environment_type == environment_type,
                    )
                    .order_by(SkillVersionModel.updated_at.desc())
                )
            ).all()
            definitions = {
                row.id: row
                for row in (
                    await session.scalars(
                        select(SkillDefinitionModel).where(
                            SkillDefinitionModel.tenant_id == tenant_id,
                            SkillDefinitionModel.environment_type == environment_type,
                        )
                    )
                ).all()
            }
        return tuple(
            self._version_from(row)
            for row in rows
            if set(definitions[row.skill_id].capabilities_json or ()).issubset(
                capabilities
            )
        )

    async def _activate(
        self,
        session: AsyncSession,
        version: SkillVersionModel,
        *,
        reviewed_by: str,
        note: str,
    ) -> None:
        definition = await session.scalar(
            select(SkillDefinitionModel)
            .where(
                SkillDefinitionModel.tenant_id == version.tenant_id,
                SkillDefinitionModel.id == version.skill_id,
            )
            .with_for_update()
        )
        if definition is None:
            raise RuntimeError("skill version references missing definition")
        current_versions = (
            await session.scalars(
                select(SkillVersionModel)
                .where(
                    SkillVersionModel.tenant_id == version.tenant_id,
                    SkillVersionModel.skill_id == version.skill_id,
                    SkillVersionModel.status == ACTIVE_STATUS,
                    SkillVersionModel.id != version.id,
                )
                .with_for_update()
            )
        ).all()
        for current in current_versions:
            current.status = ARCHIVED_STATUS
        version.status = ACTIVE_STATUS
        version.reviewed_by = reviewed_by
        version.review_note = note
        version.decided_at = datetime.now(UTC)
        definition.active_version_id = version.id

    @staticmethod
    async def _locked_version(
        session: AsyncSession, tenant_id: str, version_id: str
    ) -> SkillVersionModel | None:
        return await session.scalar(
            select(SkillVersionModel)
            .where(
                SkillVersionModel.tenant_id == tenant_id,
                SkillVersionModel.id == version_id,
            )
            .with_for_update()
        )

    @staticmethod
    async def _version(
        session: AsyncSession, tenant_id: str, version_id: str
    ) -> SkillVersionModel | None:
        return await session.scalar(
            select(SkillVersionModel).where(
                SkillVersionModel.tenant_id == tenant_id,
                SkillVersionModel.id == version_id,
            )
        )

    @staticmethod
    def _validate_manifest(
        manifest: dict[str, object], capabilities: tuple[str, ...]
    ) -> None:
        if not str(manifest.get("name") or "").strip():
            raise ValueError("skill manifest requires name")
        manifest_capabilities = manifest.get("capabilities")
        if not isinstance(manifest_capabilities, list) or not all(
            isinstance(item, str) and item.strip() for item in manifest_capabilities
        ):
            raise ValueError("skill manifest requires capabilities")
        if set(manifest_capabilities) != set(capabilities):
            raise ValueError("skill manifest capabilities must match definition")
        if any(not capability.endswith(".read") for capability in capabilities):
            raise ValueError("skill capabilities must be readonly in V1")
        if manifest.get("creates_tool") or manifest.get("script"):
            raise ValueError("skill manifest cannot create tools or scripts")

    @staticmethod
    def _checksum(manifest: dict[str, object], procedure: dict[str, object]) -> str:
        encoded = json.dumps(
            {"manifest": manifest, "procedure": procedure},
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _definition_from(model: SkillDefinitionModel) -> SkillDefinition:
        return SkillDefinition(
            skill_id=model.id,
            tenant_id=model.tenant_id,
            name=model.name,
            owner=model.owner,
            environment_type=model.environment_type,
            capabilities=tuple(model.capabilities_json or ()),
            active_version_id=model.active_version_id,
        )

    @staticmethod
    def _version_from(model: SkillVersionModel) -> SkillVersion:
        return SkillVersion(
            version_id=model.id,
            tenant_id=model.tenant_id,
            skill_id=model.skill_id,
            version=model.version,
            status=model.status,
            manifest=dict(model.manifest_json or {}),
            procedure=dict(model.procedure_json or {}),
            checksum=model.checksum,
            source_task_id=model.source_task_id,
            benchmark_report_id=model.benchmark_report_id,
            created_by=model.created_by,
            reviewed_by=model.reviewed_by,
            review_note=model.review_note,
        )
