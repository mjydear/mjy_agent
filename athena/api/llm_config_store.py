"""Tenant-scoped metadata storage for Web Console LLM configurations."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import LLMConfigModel
from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json
from athena.infra.secret_store import (
    LocalEncryptedSecretStore,
    SecretStore,
    _run_blocking,
)

if TYPE_CHECKING:
    from athena.api.auth import TenantContext


_KEY_PREFIX = "llm-config:v2"


def _tenant_id(tenant: "TenantContext") -> str:
    tenant_id = getattr(tenant, "tenant_id", "")
    if not isinstance(tenant_id, str) or not tenant_id.strip():
        raise ValueError("tenant must provide a non-empty tenant_id")
    return tenant_id.strip()


def _credential_suffix(secret: str | None) -> str | None:
    if not secret or len(secret) < 8:
        return None
    return secret[-4:]


@dataclass(frozen=True)
class StoredLLMConfig:
    config_id: str
    provider: str
    display_name: str
    model: str
    credential_ref: str | None = None
    credential_suffix: str | None = None
    base_url: str | None = None
    enabled: bool = True
    is_default: bool = False
    status: str = "available"


class LLMConfigStateError(RuntimeError):
    """Raised when a lifecycle transition would select an unusable config."""

    def __init__(self, error_code: str) -> None:
        super().__init__(error_code)
        self.error_code = error_code


class LLMConfigStore:
    """Store only credential metadata in cache and secrets behind references."""

    def __init__(
        self, cache: CacheBackend, secret_store: SecretStore | None = None
    ) -> None:
        self._cache = cache
        self._secrets = secret_store or LocalEncryptedSecretStore(cache)

    @staticmethod
    def _key(tenant_id: str, config_id: str) -> str:
        return f"{_KEY_PREFIX}:{tenant_id}:{config_id}"

    @staticmethod
    def _index_key(tenant_id: str) -> str:
        return f"{_KEY_PREFIX}:index:{tenant_id}"

    def _index(self, tenant: "TenantContext") -> list[str]:
        raw = cache_get_json(self._cache, self._index_key(_tenant_id(tenant))) or []
        return [str(config_id) for config_id in raw]

    def get(self, tenant: "TenantContext", config_id: str) -> StoredLLMConfig | None:
        raw = cache_get_json(self._cache, self._key(_tenant_id(tenant), config_id))
        if not raw:
            return None
        if "api_key" in raw:
            raise RuntimeError("plaintext LLM credentials are not supported")
        return StoredLLMConfig(**raw)

    def list(self, tenant: "TenantContext") -> list[StoredLLMConfig]:
        return [
            item
            for config_id in self._index(tenant)
            if (item := self.get(tenant, config_id)) is not None
        ]

    def save(self, tenant: "TenantContext", config: StoredLLMConfig) -> StoredLLMConfig:
        tenant_id = _tenant_id(tenant)
        ids = self._index(tenant)
        if config.config_id not in ids:
            ids.append(config.config_id)
        if config.is_default:
            for current in self.list(tenant):
                if current.config_id != config.config_id and current.is_default:
                    updated = replace(current, is_default=False)
                    cache_set_json(
                        self._cache,
                        self._key(tenant_id, current.config_id),
                        asdict(updated),
                    )
        cache_set_json(
            self._cache, self._key(tenant_id, config.config_id), asdict(config)
        )
        cache_set_json(self._cache, self._index_key(tenant_id), ids)
        return config

    def create(
        self,
        tenant: "TenantContext",
        *,
        provider: str,
        display_name: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        enabled: bool = True,
        is_default: bool = False,
        status: str = "available",
    ) -> StoredLLMConfig:
        tenant_id = _tenant_id(tenant)
        existing = self.list(tenant)
        make_default = enabled and (
            is_default or not any(item.enabled and item.is_default for item in existing)
        )
        credential_ref = None
        if api_key:
            credential_ref = self._secrets.put(tenant_id, api_key)
        config = StoredLLMConfig(
            config_id=f"llm-{uuid.uuid4().hex[:12]}",
            provider=provider,
            display_name=display_name,
            model=model,
            credential_ref=credential_ref,
            credential_suffix=_credential_suffix(api_key),
            base_url=base_url,
            enabled=enabled,
            is_default=make_default,
            status=status,
        )
        try:
            return self.save(tenant, config)
        except Exception:
            if credential_ref:
                self._secrets.delete(tenant_id, credential_ref)
            raise

    def resolve_api_key(
        self, tenant: "TenantContext", config: StoredLLMConfig
    ) -> str | None:
        if not config.credential_ref:
            return None
        return self._secrets.get(_tenant_id(tenant), config.credential_ref)

    def record_connection_result(
        self, tenant: "TenantContext", config_id: str, *, success: bool
    ) -> StoredLLMConfig | None:
        """Persist only the outcome of a connection probe, never its details."""
        config = self.get(tenant, config_id)
        if config is None:
            return None
        return self.save(
            tenant,
            replace(config, status="available" if success else "unavailable"),
        )

    def set_default(
        self, tenant: "TenantContext", config_id: str
    ) -> StoredLLMConfig | None:
        config = self.get(tenant, config_id)
        if config is None:
            return None
        if not config.enabled:
            raise LLMConfigStateError("LLM_CONFIG_DISABLED")
        if config.status != "available":
            raise LLMConfigStateError("LLM_CONFIG_UNAVAILABLE")
        return self.save(tenant, replace(config, is_default=True))

    def disable(
        self, tenant: "TenantContext", config_id: str
    ) -> StoredLLMConfig | None:
        """Disable a config and move the default pointer to a usable peer."""
        config = self.get(tenant, config_id)
        if config is None:
            return None

        disabled = self.save(tenant, replace(config, enabled=False, is_default=False))
        if config.is_default:
            for candidate in self.list(tenant):
                if (
                    candidate.config_id != config_id
                    and candidate.enabled
                    and candidate.status == "available"
                ):
                    self.save(tenant, replace(candidate, is_default=True))
                    break
        return disabled

    def rotate_credential(
        self, tenant: "TenantContext", config_id: str, api_key: str
    ) -> StoredLLMConfig | None:
        """Replace a credential reference without writing the secret to metadata."""
        config = self.get(tenant, config_id)
        if config is None:
            return None

        tenant_id = _tenant_id(tenant)
        new_ref = self._secrets.put(tenant_id, api_key)
        updated = replace(
            config,
            credential_ref=new_ref,
            credential_suffix=_credential_suffix(api_key),
        )
        try:
            # Delete the previous credential before publishing the new reference.
            # A crash in this small local-store window leaves the config unusable,
            # which is preferable to keeping a rotated credential active.
            if config.credential_ref:
                self._secrets.delete(tenant_id, config.credential_ref)
            return self.save(tenant, updated)
        except Exception:
            try:
                self._secrets.delete(tenant_id, new_ref)
            except Exception:  # noqa: BLE001 - cleanup must not mask the failure.
                pass
            raise

    def delete(self, tenant: "TenantContext", config_id: str) -> bool:
        tenant_id = _tenant_id(tenant)
        config = self.get(tenant, config_id)
        if config is None:
            return False
        if config.credential_ref:
            self._secrets.delete(tenant_id, config.credential_ref)
        self._cache.delete(self._key(tenant_id, config_id))
        remaining = [item for item in self._index(tenant) if item != config_id]
        cache_set_json(self._cache, self._index_key(tenant_id), remaining)
        if config.is_default:
            for replacement_id in remaining:
                replacement = self.get(tenant, replacement_id)
                if (
                    replacement
                    and replacement.enabled
                    and replacement.status == "available"
                ):
                    self.save(tenant, replace(replacement, is_default=True))
                    break
        return True


class DurableLLMConfigStore:
    """Tenant-scoped durable LLM metadata store backed by SQLAlchemy."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        secret_store: SecretStore,
    ) -> None:
        self._sessions = sessions
        self._secrets = secret_store

    def get(self, tenant: "TenantContext", config_id: str) -> StoredLLMConfig | None:
        return _run_blocking(self._get_async(_tenant_id(tenant), config_id))  # type: ignore[return-value]

    async def _get_async(
        self, tenant_id: str, config_id: str
    ) -> StoredLLMConfig | None:
        async with self._sessions() as session:
            model = await self._select(session, tenant_id, config_id)
        return self._from(model) if model is not None else None

    def list(self, tenant: "TenantContext") -> list[StoredLLMConfig]:
        return list(_run_blocking(self._list_async(_tenant_id(tenant))))  # type: ignore[arg-type]

    async def _list_async(self, tenant_id: str) -> tuple[StoredLLMConfig, ...]:
        async with self._sessions() as session:
            rows = (
                await session.scalars(
                    select(LLMConfigModel)
                    .where(LLMConfigModel.tenant_id == tenant_id)
                    .order_by(LLMConfigModel.created_at.desc())
                )
            ).all()
        return tuple(self._from(row) for row in rows)

    def save(self, tenant: "TenantContext", config: StoredLLMConfig) -> StoredLLMConfig:
        return _run_blocking(self._save_async(_tenant_id(tenant), config))  # type: ignore[return-value]

    async def _save_async(
        self, tenant_id: str, config: StoredLLMConfig
    ) -> StoredLLMConfig:
        async with self._sessions() as session:
            async with session.begin():
                if config.is_default:
                    defaults = (
                        await session.scalars(
                            select(LLMConfigModel)
                            .where(
                                LLMConfigModel.tenant_id == tenant_id,
                                LLMConfigModel.is_default.is_(True),
                                LLMConfigModel.config_id != config.config_id,
                            )
                            .with_for_update()
                        )
                    ).all()
                    for default in defaults:
                        default.is_default = False
                model = await self._select_locked(session, tenant_id, config.config_id)
                if model is None:
                    model = LLMConfigModel(
                        id=f"llm-config-{uuid.uuid4().hex}",
                        tenant_id=tenant_id,
                        config_id=config.config_id,
                    )
                    session.add(model)
                self._apply(model, config)
        return config

    def create(
        self,
        tenant: "TenantContext",
        *,
        provider: str,
        display_name: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        enabled: bool = True,
        is_default: bool = False,
        status: str = "available",
    ) -> StoredLLMConfig:
        tenant_id = _tenant_id(tenant)
        existing = self.list(tenant)
        make_default = enabled and (
            is_default or not any(item.enabled and item.is_default for item in existing)
        )
        credential_ref = None
        if api_key:
            credential_ref = self._secrets.put(tenant_id, api_key)
        config = StoredLLMConfig(
            config_id=f"llm-{uuid.uuid4().hex[:12]}",
            provider=provider,
            display_name=display_name,
            model=model,
            credential_ref=credential_ref,
            credential_suffix=_credential_suffix(api_key),
            base_url=base_url,
            enabled=enabled,
            is_default=make_default,
            status=status,
        )
        try:
            return self.save(tenant, config)
        except Exception:
            if credential_ref:
                self._secrets.delete(tenant_id, credential_ref)
            raise

    def resolve_api_key(
        self, tenant: "TenantContext", config: StoredLLMConfig
    ) -> str | None:
        if not config.credential_ref:
            return None
        return self._secrets.get(_tenant_id(tenant), config.credential_ref)

    def record_connection_result(
        self, tenant: "TenantContext", config_id: str, *, success: bool
    ) -> StoredLLMConfig | None:
        config = self.get(tenant, config_id)
        if config is None:
            return None
        return self.save(
            tenant,
            replace(config, status="available" if success else "unavailable"),
        )

    def set_default(
        self, tenant: "TenantContext", config_id: str
    ) -> StoredLLMConfig | None:
        config = self.get(tenant, config_id)
        if config is None:
            return None
        if not config.enabled:
            raise LLMConfigStateError("LLM_CONFIG_DISABLED")
        if config.status != "available":
            raise LLMConfigStateError("LLM_CONFIG_UNAVAILABLE")
        return self.save(tenant, replace(config, is_default=True))

    def disable(
        self, tenant: "TenantContext", config_id: str
    ) -> StoredLLMConfig | None:
        config = self.get(tenant, config_id)
        if config is None:
            return None

        disabled = self.save(tenant, replace(config, enabled=False, is_default=False))
        if config.is_default:
            for candidate in self.list(tenant):
                if (
                    candidate.config_id != config_id
                    and candidate.enabled
                    and candidate.status == "available"
                ):
                    self.save(tenant, replace(candidate, is_default=True))
                    break
        return disabled

    def rotate_credential(
        self, tenant: "TenantContext", config_id: str, api_key: str
    ) -> StoredLLMConfig | None:
        config = self.get(tenant, config_id)
        if config is None:
            return None

        tenant_id = _tenant_id(tenant)
        new_ref = self._secrets.put(tenant_id, api_key)
        updated = replace(
            config,
            credential_ref=new_ref,
            credential_suffix=_credential_suffix(api_key),
        )
        try:
            if config.credential_ref:
                self._secrets.delete(tenant_id, config.credential_ref)
            return self.save(tenant, updated)
        except Exception:
            try:
                self._secrets.delete(tenant_id, new_ref)
            except Exception:  # noqa: BLE001
                pass
            raise

    def delete(self, tenant: "TenantContext", config_id: str) -> bool:
        return bool(_run_blocking(self._delete_async(_tenant_id(tenant), config_id)))

    async def _delete_async(self, tenant_id: str, config_id: str) -> bool:
        async with self._sessions() as session:
            async with session.begin():
                model = await self._select_locked(session, tenant_id, config_id)
                if model is None:
                    return False
                was_default = bool(model.is_default)
                credential_ref = model.credential_ref
                await session.delete(model)
        if credential_ref:
            self._secrets.delete(tenant_id, credential_ref)
        if was_default:
            for replacement in self.list(_FakeTenant(tenant_id)):
                if replacement.enabled and replacement.status == "available":
                    self.save(
                        _FakeTenant(tenant_id), replace(replacement, is_default=True)
                    )
                    break
        return True

    @staticmethod
    async def _select(
        session: AsyncSession, tenant_id: str, config_id: str
    ) -> LLMConfigModel | None:
        return await session.scalar(
            select(LLMConfigModel).where(
                LLMConfigModel.tenant_id == tenant_id,
                LLMConfigModel.config_id == config_id,
            )
        )

    @staticmethod
    async def _select_locked(
        session: AsyncSession, tenant_id: str, config_id: str
    ) -> LLMConfigModel | None:
        return await session.scalar(
            select(LLMConfigModel)
            .where(
                LLMConfigModel.tenant_id == tenant_id,
                LLMConfigModel.config_id == config_id,
            )
            .with_for_update()
        )

    @staticmethod
    def _apply(model: LLMConfigModel, config: StoredLLMConfig) -> None:
        model.provider = config.provider
        model.display_name = config.display_name
        model.model = config.model
        model.credential_ref = config.credential_ref
        model.credential_suffix = config.credential_suffix
        model.base_url = config.base_url
        model.enabled = config.enabled
        model.is_default = config.is_default
        model.status = config.status

    @staticmethod
    def _from(model: LLMConfigModel) -> StoredLLMConfig:
        return StoredLLMConfig(
            config_id=model.config_id,
            provider=model.provider,
            display_name=model.display_name,
            model=model.model,
            credential_ref=model.credential_ref,
            credential_suffix=model.credential_suffix,
            base_url=model.base_url,
            enabled=bool(model.enabled),
            is_default=bool(model.is_default),
            status=model.status,
        )


class _FakeTenant:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
