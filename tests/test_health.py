"""健康探针 /healthz /readyz 测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.config import (
    AthenaSettings,
    CacheSettings,
    DatabaseSettings,
    K8sSettings,
    OpsSettings,
    RuntimeSettings,
    SecuritySettings,
    WebSettings,
)


def _client() -> TestClient:
    # 不注入 service：走真实 create_app 装配，验证探针与 state 装配
    return TestClient(create_app())


def test_healthz_alive() -> None:
    with _client() as client:
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "alive"


def test_readyz_ready_by_default() -> None:
    with _client() as client:
        resp = client.get("/readyz")
        assert resp.status_code == 200
        assert resp.json()["ready"] is True
        assert all(
            {
                "configured_backend",
                "active_backend",
                "status",
                "reason_code",
            }.issubset(component)
            for component in resp.json()["components"]
        )


def test_production_profile_rejects_unsafe_configuration() -> None:
    settings = AthenaSettings(runtime=RuntimeSettings(profile="production"))
    with TestClient(create_app(settings=settings)) as client:
        resp = client.get("/readyz")

    assert resp.status_code == 503
    payload = resp.json()
    assert payload["ready"] is False
    assert payload["reason_code"] == "AUTH_REQUIRED"
    assert payload["profile"] == "production"


def test_production_profile_is_not_ready_after_cache_and_live_degradation() -> None:
    settings = AthenaSettings(
        runtime=RuntimeSettings(profile="production"),
        web=WebSettings(cors_origins=["https://athena.example.test"]),
        security=SecuritySettings(
            require_auth=True,
            api_keys={"test-key": "tenant-a"},
            alert_integration_tokens={"alert-token": "tenant-a"},
            roles={"tenant-a": ["ops:read", "ops:run"]},
            secret_master_key="test-secret-master-key",
        ),
        cache=CacheSettings(redis_url="redis://127.0.0.1:1/0"),
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:"),
        ops=OpsSettings(
            mode="real",
            kubernetes=K8sSettings(
                namespace_allowlist=["default"],
                fallback_policy="fail_closed",
                timeout=0.1,
            ),
        ),
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        resp = client.get("/readyz")

    assert resp.status_code == 503
    components = {item["component"]: item for item in resp.json()["components"]}
    assert components["configuration"]["status"] == "healthy"
    assert components["cache"] == {
        "component": "cache",
        "configured_backend": "redis",
        "active_backend": "memory",
        "status": "degraded",
        "reason_code": "CACHE_FALLBACK_TO_MEMORY",
    }
    assert components["kubernetes"]["active_backend"] == "unavailable"


def test_readyz_returns_503_when_draining() -> None:
    with _client() as client:
        client.app.state.draining = True
        resp = client.get("/readyz")
        assert resp.status_code == 503
        assert resp.json()["ready"] is False


def test_lifespan_sets_draining_on_shutdown() -> None:
    app = create_app()
    with TestClient(app):
        assert app.state.draining is False
    # 退出 with 触发 lifespan 关闭钩子
    assert app.state.draining is True
