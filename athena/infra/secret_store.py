"""Tenant-scoped encrypted secret storage contracts and local adapter."""

from __future__ import annotations

import base64
import hashlib
import asyncio
import json
import os
import threading
import uuid
from typing import Protocol

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import SecretRecordModel
from athena.infra.cache import CacheBackend


class SecretStoreError(RuntimeError):
    """Base error for secret storage without exposing secret material."""


class SecretNotFoundError(SecretStoreError):
    """Raised when a credential reference does not exist for a tenant."""


class SecretIntegrityError(SecretStoreError):
    """Raised when encrypted secret content cannot be authenticated."""


class SecretStore(Protocol):
    """Minimal tenant-scoped secret store used by configuration repositories."""

    def put(
        self,
        tenant_id: str,
        secret: str,
        *,
        credential_ref: str | None = None,
    ) -> str: ...

    def get(self, tenant_id: str, credential_ref: str) -> str: ...

    def delete(self, tenant_id: str, credential_ref: str) -> None: ...


_PROCESS_LOCAL_MASTER_KEY = Fernet.generate_key()


def _require_identifier(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _fernet_key(master_key: str | bytes | None) -> bytes:
    configured = (
        master_key if master_key is not None else os.getenv("ATHENA_SECRET_MASTER_KEY")
    )
    if configured is None:
        # Demo/test fallback only. Persistent deployments must inject a stable key.
        return _PROCESS_LOCAL_MASTER_KEY
    raw = configured.encode("utf-8") if isinstance(configured, str) else configured
    if not raw:
        raise ValueError("master_key must not be empty")
    try:
        decoded = base64.urlsafe_b64decode(raw)
    except (ValueError, TypeError):
        decoded = b""
    if len(decoded) == 32:
        return raw
    return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())


def _run_blocking(awaitable: object) -> object:
    """Run an async DB operation from sync API/service code."""

    async def _await() -> object:
        return await awaitable  # type: ignore[misc]

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_await())

    result: dict[str, object] = {}
    error: dict[str, BaseException] = {}

    def _thread_main() -> None:
        try:
            result["value"] = asyncio.run(_await())
        except BaseException as exc:  # noqa: BLE001 - tunnel to caller thread.
            error["error"] = exc

    thread = threading.Thread(target=_thread_main, daemon=True)
    thread.start()
    thread.join()
    if error:
        raise error["error"]
    return result.get("value")


class LocalEncryptedSecretStore:
    """Fernet-authenticated encrypted secrets over an injected local backend.

    The backend only receives ciphertext. A stable high-entropy master key must be
    injected through the constructor or ``ATHENA_SECRET_MASTER_KEY`` when secrets
    need to survive process restarts.
    """

    def __init__(
        self,
        backend: CacheBackend,
        *,
        master_key: str | bytes | None = None,
        key_prefix: str = "secrets:v1",
    ) -> None:
        self._backend = backend
        self._fernet = Fernet(_fernet_key(master_key))
        self._key_prefix = _require_identifier(key_prefix, "key_prefix")

    def put(
        self,
        tenant_id: str,
        secret: str,
        *,
        credential_ref: str | None = None,
    ) -> str:
        tenant_id = _require_identifier(tenant_id, "tenant_id")
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret must be a non-empty string")
        credential_ref = _require_identifier(
            credential_ref or f"credential-{uuid.uuid4().hex}", "credential_ref"
        )
        envelope = json.dumps(
            {
                "tenant_id": tenant_id,
                "credential_ref": credential_ref,
                "secret": secret,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        token = self._fernet.encrypt(envelope).decode("ascii")
        self._backend.set(self._storage_key(tenant_id, credential_ref), token)
        return credential_ref

    def get(self, tenant_id: str, credential_ref: str) -> str:
        tenant_id = _require_identifier(tenant_id, "tenant_id")
        credential_ref = _require_identifier(credential_ref, "credential_ref")
        token = self._backend.get(self._storage_key(tenant_id, credential_ref))
        if token is None:
            raise SecretNotFoundError("credential is unavailable")
        try:
            envelope = json.loads(self._fernet.decrypt(token.encode("ascii")))
        except (
            InvalidToken,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise SecretIntegrityError(
                "credential integrity verification failed"
            ) from exc
        if not isinstance(envelope, dict):
            raise SecretIntegrityError("credential envelope is invalid")
        if (
            envelope.get("tenant_id") != tenant_id
            or envelope.get("credential_ref") != credential_ref
            or not isinstance(envelope.get("secret"), str)
            or not envelope.get("secret")
        ):
            raise SecretIntegrityError("credential scope verification failed")
        return str(envelope["secret"])

    def delete(self, tenant_id: str, credential_ref: str) -> None:
        tenant_id = _require_identifier(tenant_id, "tenant_id")
        credential_ref = _require_identifier(credential_ref, "credential_ref")
        self._backend.delete(self._storage_key(tenant_id, credential_ref))

    def _storage_key(self, tenant_id: str, credential_ref: str) -> str:
        scoped_ref = hashlib.sha256(
            f"{tenant_id}\0{credential_ref}".encode("utf-8")
        ).hexdigest()
        return f"{self._key_prefix}:{scoped_ref}"


class DurableEncryptedSecretStore:
    """Fernet-authenticated encrypted secrets stored in the durable database."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        master_key: str | bytes | None = None,
        key_version: str = "local-fernet-v1",
    ) -> None:
        self._sessions = sessions
        self._fernet = Fernet(_fernet_key(master_key))
        self._key_version = _require_identifier(key_version, "key_version")

    def put(
        self,
        tenant_id: str,
        secret: str,
        *,
        credential_ref: str | None = None,
    ) -> str:
        return str(
            _run_blocking(
                self._put_async(tenant_id, secret, credential_ref=credential_ref)
            )
        )

    async def _put_async(
        self,
        tenant_id: str,
        secret: str,
        *,
        credential_ref: str | None = None,
    ) -> str:
        tenant_id = _require_identifier(tenant_id, "tenant_id")
        if not isinstance(secret, str) or not secret:
            raise ValueError("secret must be a non-empty string")
        credential_ref = _require_identifier(
            credential_ref or f"credential-{uuid.uuid4().hex}", "credential_ref"
        )
        envelope = json.dumps(
            {
                "tenant_id": tenant_id,
                "credential_ref": credential_ref,
                "secret": secret,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        ciphertext = self._fernet.encrypt(envelope).decode("ascii")
        async with self._sessions() as session:
            async with session.begin():
                model = await self._locked(session, tenant_id, credential_ref)
                if model is None:
                    session.add(
                        SecretRecordModel(
                            id=f"secret-{uuid.uuid4().hex}",
                            tenant_id=tenant_id,
                            credential_ref=credential_ref,
                            key_version=self._key_version,
                            ciphertext=ciphertext,
                        )
                    )
                else:
                    model.key_version = self._key_version
                    model.ciphertext = ciphertext
        return credential_ref

    def get(self, tenant_id: str, credential_ref: str) -> str:
        return str(_run_blocking(self._get_async(tenant_id, credential_ref)))

    async def _get_async(self, tenant_id: str, credential_ref: str) -> str:
        tenant_id = _require_identifier(tenant_id, "tenant_id")
        credential_ref = _require_identifier(credential_ref, "credential_ref")
        async with self._sessions() as session:
            model = await session.scalar(
                select(SecretRecordModel).where(
                    SecretRecordModel.tenant_id == tenant_id,
                    SecretRecordModel.credential_ref == credential_ref,
                )
            )
        if model is None:
            raise SecretNotFoundError("credential is unavailable")
        try:
            envelope = json.loads(
                self._fernet.decrypt(model.ciphertext.encode("ascii"))
            )
        except (
            InvalidToken,
            UnicodeError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ) as exc:
            raise SecretIntegrityError(
                "credential integrity verification failed"
            ) from exc
        if not isinstance(envelope, dict):
            raise SecretIntegrityError("credential envelope is invalid")
        if (
            envelope.get("tenant_id") != tenant_id
            or envelope.get("credential_ref") != credential_ref
            or not isinstance(envelope.get("secret"), str)
            or not envelope.get("secret")
        ):
            raise SecretIntegrityError("credential scope verification failed")
        return str(envelope["secret"])

    def delete(self, tenant_id: str, credential_ref: str) -> None:
        _run_blocking(self._delete_async(tenant_id, credential_ref))

    async def _delete_async(self, tenant_id: str, credential_ref: str) -> None:
        tenant_id = _require_identifier(tenant_id, "tenant_id")
        credential_ref = _require_identifier(credential_ref, "credential_ref")
        async with self._sessions() as session:
            async with session.begin():
                model = await self._locked(session, tenant_id, credential_ref)
                if model is not None:
                    await session.delete(model)

    @staticmethod
    async def _locked(
        session: AsyncSession, tenant_id: str, credential_ref: str
    ) -> SecretRecordModel | None:
        return await session.scalar(
            select(SecretRecordModel)
            .where(
                SecretRecordModel.tenant_id == tenant_id,
                SecretRecordModel.credential_ref == credential_ref,
            )
            .with_for_update()
        )
