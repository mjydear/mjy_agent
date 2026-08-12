"""Focused P2-02 tests for encrypted, tenant-scoped LLM credentials."""

from __future__ import annotations

import json
import os
import sqlite3
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from athena.agent import ReActAgent
from athena.api.auth import TenantContext
from athena.api.llm_config_store import LLMConfigStore
from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings, RuntimeSettings, production_readiness_issues
from athena.infra.cache import InMemoryCache
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.infra.secret_store import (
    LocalEncryptedSecretStore,
    SecretIntegrityError,
    SecretNotFoundError,
)
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry


class _StaticLLM(LLMClient):
    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        return LLMResponse(
            content=(
                '{"thought":"done","action":null,"action_input":{},'
                '"final_answer":"ok"}'
            ),
            model="static",
        )


def _agent() -> ReActAgent:
    return ReActAgent(
        llm_client=_StaticLLM(),
        prompt_assembler=ContextAssembler(),
        tool_registry=ToolRegistry(),
        memory=WorkingMemory(),
        max_steps=1,
    )


def _tenant(tenant_id: str) -> TenantContext:
    return TenantContext(tenant_id=tenant_id, api_key=None, roles=("*",))


def _cached_values(cache: InMemoryCache) -> str:
    return "\n".join(value for value, _ in cache._store.values())


def _d_drive_sqlite_url(name: str) -> tuple[str, Path]:
    root = Path(os.getenv("ATHENA_TEST_TMP", "D:/tmp/mjy_agent/tests"))
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{name}-{uuid.uuid4().hex}.db"
    return f"sqlite:///{path.as_posix()}", path


def test_local_secret_store_encrypts_and_authenticates_content() -> None:
    cache = InMemoryCache(namespace="secret-store-test")
    store = LocalEncryptedSecretStore(cache, master_key=Fernet.generate_key())

    credential_ref = store.put("tenant-a", "sk-super-secret")

    assert store.get("tenant-a", credential_ref) == "sk-super-secret"
    assert "sk-super-secret" not in _cached_values(cache)
    with pytest.raises(SecretNotFoundError):
        store.get("tenant-b", credential_ref)

    storage_key = next(iter(cache._store))
    token, expires_at = cache._store[storage_key]
    replacement = "A" if token[-1] != "A" else "B"
    cache._store[storage_key] = (token[:-1] + replacement, expires_at)
    with pytest.raises(SecretIntegrityError):
        store.get("tenant-a", credential_ref)


def test_local_secret_store_rejects_an_explicit_empty_master_key() -> None:
    with pytest.raises(ValueError, match="master_key"):
        LocalEncryptedSecretStore(
            InMemoryCache(namespace="empty-key-test"), master_key=b""
        )


def test_production_requires_a_stable_secret_master_key() -> None:
    settings = AthenaSettings(runtime=RuntimeSettings(profile="production"))
    assert "SECRET_MASTER_KEY_REQUIRED" in production_readiness_issues(settings)
    settings.security.secret_master_key = Fernet.generate_key().decode("ascii")
    assert "SECRET_MASTER_KEY_REQUIRED" not in production_readiness_issues(settings)


def test_production_rejects_legacy_request_credential() -> None:
    settings = AthenaSettings(runtime=RuntimeSettings(profile="production"))
    settings.security.secret_master_key = Fernet.generate_key().decode("ascii")
    client = TestClient(create_app(settings=settings, service=None))
    session_id = client.post("/api/sessions", json={"title": "managed"}).json()[
        "session"
    ]["session_id"]
    response = client.post(
        "/api/chat",
        json={
            "session_id": session_id,
            "message": "hello",
            "llm_config": {"provider": "litellm", "model": "test", "api_key": "secret"},
        },
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "LLM_LEGACY_CREDENTIAL_FORBIDDEN"


def test_llm_config_cache_contains_only_tenant_scoped_metadata() -> None:
    cache = InMemoryCache(namespace="llm-config-test")
    secrets = LocalEncryptedSecretStore(cache, master_key=Fernet.generate_key())
    store = LLMConfigStore(cache, secrets)
    tenant_a = _tenant("tenant-a")
    tenant_b = _tenant("tenant-b")

    config = store.create(
        tenant_a,
        provider="deepseek",
        display_name="DeepSeek",
        model="deepseek/deepseek-chat",
        api_key="sk-tenant-a-secret",
    )

    raw = cache.get(f"llm-config:v2:tenant-a:{config.config_id}")
    assert raw is not None
    metadata = json.loads(raw)
    assert "api_key" not in metadata
    assert metadata["credential_ref"] == config.credential_ref
    assert metadata["credential_suffix"] == "cret"
    assert "sk-tenant-a-secret" not in _cached_values(cache)
    assert store.resolve_api_key(tenant_a, config) == "sk-tenant-a-secret"
    assert store.get(tenant_b, config.config_id) is None
    assert store.list(tenant_b) == []


def test_deleting_config_removes_credential() -> None:
    cache = InMemoryCache(namespace="llm-config-delete-test")
    secrets = LocalEncryptedSecretStore(cache, master_key=Fernet.generate_key())
    store = LLMConfigStore(cache, secrets)
    tenant = _tenant("tenant-a")
    config = store.create(
        tenant,
        provider="openai",
        display_name="OpenAI",
        model="gpt-4o",
        api_key="sk-delete-secret",
    )

    assert store.delete(tenant, config.config_id) is True
    with pytest.raises(SecretNotFoundError):
        secrets.get("tenant-a", str(config.credential_ref))


def test_llm_config_api_is_tenant_scoped_and_never_returns_plaintext() -> None:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-a": "tenant-a", "key-b": "tenant-b"}
    service = AthenaWebService(agent_factory=_agent, session_ttl_seconds=60)
    client = TestClient(create_app(settings=settings, service=service))

    response = client.post(
        "/api/llm/configs",
        headers={"X-API-Key": "key-a"},
        json={
            "provider": "deepseek",
            "display_name": "DeepSeek",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-api-secret",
        },
    )

    assert response.status_code == 201
    public = response.json()
    assert public["has_api_key"] is True
    assert public["masked_api_key"] == "****cret"
    assert "sk-api-secret" not in response.text
    assert client.get("/api/llm/configs", headers={"X-API-Key": "key-b"}).json() == []
    assert (
        client.delete(
            f"/api/llm/configs/{public['config_id']}",
            headers={"X-API-Key": "key-b"},
        ).status_code
        == 404
    )
    listed = client.get("/api/llm/configs", headers={"X-API-Key": "key-a"}).json()
    assert listed == [public]


def test_llm_config_validation_error_does_not_echo_credential() -> None:
    client = TestClient(
        create_app(
            service=AthenaWebService(agent_factory=_agent, session_ttl_seconds=60)
        )
    )
    oversized = "sk-" + "sensitive" * 40

    response = client.post(
        "/api/llm/configs",
        json={
            "provider": "deepseek",
            "display_name": "DeepSeek",
            "model": "deepseek/deepseek-chat",
            "api_key": oversized,
        },
    )

    assert response.status_code == 400
    assert response.json()["error_code"] == "LLM_CREDENTIAL_INVALID"
    assert oversized not in response.text


def test_chat_resolves_managed_credential_only_for_owning_tenant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_build_agent(**kwargs: object) -> ReActAgent:
        captured.update(kwargs)
        return _agent()

    monkeypatch.setattr("athena.bootstrap.build_agent", fake_build_agent)
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-a": "tenant-a", "key-b": "tenant-b"}
    service = AthenaWebService(agent_factory=_agent, session_ttl_seconds=60)
    client = TestClient(create_app(settings=settings, service=service))
    public = client.post(
        "/api/llm/configs",
        headers={"X-API-Key": "key-a"},
        json={
            "provider": "deepseek",
            "display_name": "DeepSeek",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-managed-secret",
        },
    ).json()
    session_id = client.post(
        "/api/sessions",
        headers={"X-API-Key": "key-a"},
        json={"title": "managed"},
    ).json()["session"]["session_id"]

    response = client.post(
        "/api/chat",
        headers={"X-API-Key": "key-a"},
        json={
            "session_id": session_id,
            "message": "hello",
            "llm_config_id": public["config_id"],
        },
    )

    assert response.status_code == 200
    assert captured["llm_api_key"] == "sk-managed-secret"
    assert "sk-managed-secret" not in repr(service._agent_config_signatures)
    forbidden = client.post(
        "/api/chat",
        headers={"X-API-Key": "key-b"},
        json={
            "session_id": session_id,
            "message": "hello",
            "llm_config_id": public["config_id"],
        },
    )
    assert forbidden.status_code == 400
    assert forbidden.json()["error_code"] == "LLM_CONFIG_UNAVAILABLE"


def test_llm_config_lifecycle_is_tenant_scoped_and_never_echoes_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def successful_probe(config: object, api_key: str | None) -> None:
        assert getattr(config, "config_id")
        assert api_key == "sk-original-secret"

    monkeypatch.setattr(
        "athena.api.routes.llm_configs._probe_connection", successful_probe
    )
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-a": "tenant-a", "key-b": "tenant-b"}
    service = AthenaWebService(agent_factory=_agent, session_ttl_seconds=60)
    client = TestClient(create_app(settings=settings, service=service))
    tenant_a_headers = {"X-API-Key": "key-a"}
    tenant_b_headers = {"X-API-Key": "key-b"}

    primary = client.post(
        "/api/llm/configs",
        headers=tenant_a_headers,
        json={
            "provider": "deepseek",
            "display_name": "Primary",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-original-secret",
        },
    ).json()
    backup = client.post(
        "/api/llm/configs",
        headers=tenant_a_headers,
        json={
            "provider": "deepseek",
            "display_name": "Backup",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-backup-secret",
        },
    ).json()
    primary_id = primary["config_id"]
    backup_id = backup["config_id"]

    tested = client.post(
        f"/api/llm/configs/{primary_id}/test", headers=tenant_a_headers
    )
    assert tested.status_code == 200
    assert tested.json()["success"] is True
    assert tested.json()["reason_code"] == "LLM_CONNECTION_AVAILABLE"
    assert "sk-original-secret" not in tested.text

    defaulted = client.post(
        f"/api/llm/configs/{backup_id}/default", headers=tenant_a_headers
    )
    assert defaulted.status_code == 200
    assert defaulted.json()["is_default"] is True

    disabled = client.post(
        f"/api/llm/configs/{backup_id}/disable", headers=tenant_a_headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    assert disabled.json()["is_default"] is False
    configs = client.get("/api/llm/configs", headers=tenant_a_headers).json()
    assert next(item for item in configs if item["config_id"] == primary_id)[
        "is_default"
    ]
    assert (
        client.post(
            f"/api/llm/configs/{backup_id}/default", headers=tenant_a_headers
        ).json()["error_code"]
        == "LLM_CONFIG_DISABLED"
    )

    old_config = service.llm_config_store.get(_tenant("tenant-a"), primary_id)
    assert old_config is not None
    old_credential_ref = old_config.credential_ref
    rotated = client.post(
        f"/api/llm/configs/{primary_id}/rotate",
        headers=tenant_a_headers,
        json={"api_key": "sk-rotated-secret"},
    )
    assert rotated.status_code == 200
    assert rotated.json()["masked_api_key"] == "****cret"
    assert "sk-rotated-secret" not in rotated.text
    stored = service.llm_config_store.get(_tenant("tenant-a"), primary_id)
    assert stored is not None
    assert service.llm_config_store.resolve_api_key(_tenant("tenant-a"), stored) == (
        "sk-rotated-secret"
    )
    with pytest.raises(SecretNotFoundError):
        service.llm_config_store._secrets.get("tenant-a", str(old_credential_ref))

    forbidden = client.post(
        f"/api/llm/configs/{primary_id}/rotate",
        headers=tenant_b_headers,
        json={"api_key": "sk-other-tenant-secret"},
    )
    assert forbidden.status_code == 404
    assert "sk-other-tenant-secret" not in forbidden.text
    assert "sk-original-secret" not in _cached_values(service.llm_config_store._cache)
    assert "sk-rotated-secret" not in _cached_values(service.llm_config_store._cache)
    oversized = "sk-" + "sensitive" * 40
    invalid_rotation = client.post(
        f"/api/llm/configs/{primary_id}/rotate",
        headers=tenant_a_headers,
        json={"api_key": oversized},
    )
    assert invalid_rotation.status_code == 400
    assert invalid_rotation.json()["error_code"] == "LLM_CREDENTIAL_INVALID"
    assert oversized not in invalid_rotation.text


def test_llm_connection_test_redacts_provider_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failing_probe(config: object, api_key: str | None) -> None:
        raise RuntimeError(f"provider rejected {api_key}")

    monkeypatch.setattr(
        "athena.api.routes.llm_configs._probe_connection", failing_probe
    )
    client = TestClient(
        create_app(
            service=AthenaWebService(agent_factory=_agent, session_ttl_seconds=60)
        )
    )
    created = client.post(
        "/api/llm/configs",
        json={
            "provider": "deepseek",
            "display_name": "Unreachable",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-connection-secret",
        },
    ).json()

    response = client.post(f"/api/llm/configs/{created['config_id']}/test")

    assert response.status_code == 200
    result = response.json()
    assert result["success"] is False
    assert result["status"] == "unavailable"
    assert result["reason_code"] == "LLM_CONNECTION_FAILED"
    assert "sk-connection-secret" not in response.text
    default_response = client.post(f"/api/llm/configs/{created['config_id']}/default")
    assert default_response.status_code == 409
    assert default_response.json()["error_code"] == "LLM_CONFIG_UNAVAILABLE"


def test_durable_llm_config_and_secret_survive_app_restart_without_plaintext() -> None:
    database_url, database_path = _d_drive_sqlite_url("llm-config-restart")
    master_key = Fernet.generate_key().decode("ascii")
    settings = AthenaSettings()
    settings.database.url = database_url
    settings.database.auto_migrate = True
    settings.security.secret_master_key = master_key
    settings.security.require_auth = True
    settings.security.api_keys = {"key-a": "tenant-a", "key-b": "tenant-b"}

    app = create_app(settings=settings, service=None)
    with TestClient(app) as client:
        created = client.post(
            "/api/llm/configs",
            headers={"X-API-Key": "key-a"},
            json={
                "provider": "deepseek",
                "display_name": "Durable DeepSeek",
                "model": "deepseek/deepseek-chat",
                "api_key": "sk-durable-secret",
            },
        )
        assert created.status_code == 201
        public = created.json()
        config_id = public["config_id"]

    restarted_app = create_app(settings=settings, service=None)
    with TestClient(restarted_app) as client:
        listed = client.get("/api/llm/configs", headers={"X-API-Key": "key-a"})
        assert listed.status_code == 200
        assert listed.json() == [public]
        assert (
            client.get("/api/llm/configs", headers={"X-API-Key": "key-b"}).json() == []
        )
        stored = restarted_app.state.service.llm_config_store.get(
            _tenant("tenant-a"), config_id
        )
        assert stored is not None
        assert (
            restarted_app.state.service.llm_config_store.resolve_api_key(
                _tenant("tenant-a"), stored
            )
            == "sk-durable-secret"
        )
        with pytest.raises(SecretNotFoundError):
            restarted_app.state.service.llm_config_store._secrets.get(
                "tenant-b", str(stored.credential_ref)
            )

    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "select provider, display_name, credential_ref from llm_configs"
        ).fetchall()
        ciphertext_rows = connection.execute(
            "select tenant_id, credential_ref, ciphertext from secret_records"
        ).fetchall()
    persisted_text = json.dumps(
        {"configs": rows, "secrets": ciphertext_rows}, ensure_ascii=True
    )
    assert "sk-durable-secret" not in persisted_text
    assert len(rows) == 1
    assert rows[0][0] == "deepseek"
    assert rows[0][1] == "Durable DeepSeek"
    assert isinstance(rows[0][2], str) and rows[0][2].startswith("credential-")
    assert ciphertext_rows and "gAAAA" in ciphertext_rows[0][2]


def test_durable_secret_store_deletes_rotated_credential() -> None:
    database_url, database_path = _d_drive_sqlite_url("llm-secret-rotate")
    settings = AthenaSettings()
    settings.database.url = database_url
    settings.database.auto_migrate = True
    settings.security.secret_master_key = Fernet.generate_key().decode("ascii")
    settings.security.require_auth = True
    settings.security.api_keys = {"key-a": "tenant-a"}

    app = create_app(settings=settings, service=None)
    with TestClient(app) as client:
        created = client.post(
            "/api/llm/configs",
            headers={"X-API-Key": "key-a"},
            json={
                "provider": "openai",
                "display_name": "OpenAI",
                "model": "gpt-4o",
                "api_key": "sk-old-durable-secret",
            },
        ).json()
        old_config = app.state.service.llm_config_store.get(
            _tenant("tenant-a"), created["config_id"]
        )
        assert old_config is not None
        old_ref = old_config.credential_ref

        rotated = client.post(
            f"/api/llm/configs/{created['config_id']}/rotate",
            headers={"X-API-Key": "key-a"},
            json={"api_key": "sk-new-durable-secret"},
        )
        assert rotated.status_code == 200
        stored = app.state.service.llm_config_store.get(
            _tenant("tenant-a"), created["config_id"]
        )
        assert stored is not None
        assert (
            app.state.service.llm_config_store.resolve_api_key(
                _tenant("tenant-a"), stored
            )
            == "sk-new-durable-secret"
        )
        with pytest.raises(SecretNotFoundError):
            app.state.service.llm_config_store._secrets.get("tenant-a", str(old_ref))

    with sqlite3.connect(database_path) as connection:
        persisted_text = json.dumps(
            connection.execute("select * from secret_records").fetchall()
        )
    assert "sk-old-durable-secret" not in persisted_text
    assert "sk-new-durable-secret" not in persisted_text
