from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.config import AthenaSettings, DatabaseSettings, SecuritySettings


def _app() -> object:
    return create_app(
        settings=AthenaSettings(
            database=DatabaseSettings(
                url="sqlite+aiosqlite:///:memory:", auto_migrate=True
            ),
            security=SecuritySettings(
                require_auth=True, api_keys={"one": "tenant-one", "two": "tenant-two"}
            ),
        )
    )


def test_environment_api_is_tenant_scoped() -> None:
    with TestClient(_app()) as client:
        created = client.post(
            "/api/environments",
            headers={"X-API-Key": "one"},
            json={
                "name": "prod-k8s",
                "environment_type": "kubernetes",
                "provider": "kind",
                "mode": "mock",
                "scope": {"namespaces": ["payments"]},
            },
        )
        assert created.status_code == 201
        environment = created.json()["data"]
        assert environment["capabilities"] == [
            "k8s.workload.read",
            "k8s.logs.read",
            "metrics.query",
        ]
        environment_id = environment["id"]
        assert (
            client.get(
                f"/api/environments/{environment_id}", headers={"X-API-Key": "two"}
            ).status_code
            == 404
        )
        tested = client.post(
            f"/api/environments/{environment_id}/test", headers={"X-API-Key": "one"}
        )
        assert tested.json()["data"]["status"] == "available"
        assert (
            client.post(
                f"/api/environments/{environment_id}/sync", headers={"X-API-Key": "one"}
            ).json()["data"]["active_backend"]
            == "mock"
        )
        changed = client.patch(
            f"/api/environments/{environment_id}",
            headers={"X-API-Key": "one"},
            json={"name": "staging-k8s"},
        )
        assert changed.json()["data"]["name"] == "staging-k8s"
        assert (
            client.delete(
                f"/api/environments/{environment_id}", headers={"X-API-Key": "one"}
            ).status_code
            == 204
        )
