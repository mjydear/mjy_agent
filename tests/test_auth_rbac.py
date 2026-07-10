"""RBAC + JWT + 租户 scope 鉴权授权测试。"""

from __future__ import annotations

import time

import jwt
from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings


def _stub_agent() -> object:
    # workflow 路由不会真正调用 agent，这里仅需一个可构造的占位对象
    return object()


def _service() -> AthenaWebService:
    # cloud-ops 现在走 Agent 大脑：注入确定性测试 Agent + Demo 诊断器，让 RBAC 测试聚焦鉴权本身，
    # 不受真实集群可用性影响。
    from athena.tools.cloud.k8s import K8sReadOnlyDiagnoser
    from tests._k8s_fakes import demo_client
    from tests.test_web_console import build_test_agent

    return AthenaWebService(
        agent_factory=_stub_agent,
        cloud_agent_factory=lambda actor: build_test_agent(),
        k8s_diagnoser=K8sReadOnlyDiagnoser(demo_client()),
        session_ttl_seconds=60,
    )


def _client(settings: AthenaSettings) -> TestClient:
    return TestClient(create_app(settings=settings, service=_service()))


def test_auth_disabled_grants_all_scopes() -> None:
    # 默认无凭证 → 鉴权关闭 → workflow:run 放行
    client = _client(AthenaSettings())
    resp = client.post("/api/workflow/run", json={"task": "巡检"})
    assert resp.status_code == 200


def test_missing_api_key_401() -> None:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-admin": "admin"}
    settings.security.roles = {"admin": ["workflow:run"]}
    client = _client(settings)
    resp = client.post("/api/workflow/run", json={"task": "巡检"})
    assert resp.status_code == 401


def test_valid_key_without_scope_403() -> None:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-view": "viewer"}
    settings.security.roles = {"viewer": []}  # 无任何 scope
    client = _client(settings)
    resp = client.post(
        "/api/workflow/run", json={"task": "巡检"}, headers={"X-API-Key": "key-view"}
    )
    assert resp.status_code == 403


def test_valid_key_with_scope_200() -> None:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-admin": "admin"}
    settings.security.roles = {"admin": ["workflow:run"]}
    client = _client(settings)
    resp = client.post(
        "/api/workflow/run", json={"task": "巡检"}, headers={"X-API-Key": "key-admin"}
    )
    assert resp.status_code == 200


def test_wildcard_scope_allows_all() -> None:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-root": "root"}
    settings.security.roles = {"root": ["*"]}
    client = _client(settings)
    resp = client.post(
        "/api/cloud-ops/run",
        json={"mode": "k8s", "task": "巡检"},
        headers={"X-API-Key": "key-root"},
    )
    assert resp.status_code == 200


def test_jwt_bearer_with_scopes_200() -> None:
    settings = AthenaSettings()
    secret = "unit-test-secret-key-32-bytes-long!!"
    settings.security.jwt_secret = secret
    token = jwt.encode(
        {"tenant": "team-a", "scopes": ["workflow:run"], "exp": int(time.time()) + 60},
        secret,
        algorithm="HS256",
    )
    client = _client(settings)
    resp = client.post(
        "/api/workflow/run",
        json={"task": "巡检"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200


def test_jwt_invalid_token_401() -> None:
    settings = AthenaSettings()
    settings.security.jwt_secret = "unit-test-secret-key-32-bytes-long!!"
    client = _client(settings)
    resp = client.post(
        "/api/workflow/run",
        json={"task": "巡检"},
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert resp.status_code == 401
