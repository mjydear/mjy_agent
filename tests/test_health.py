"""健康探针 /healthz /readyz 测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app


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
