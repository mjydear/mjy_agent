"""Tenant isolation for legacy workflow and async task compatibility routes."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings


def _client() -> TestClient:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-a": "tenant-a", "key-b": "tenant-b"}
    settings.security.roles = {
        "tenant-a": ["workflow:run"],
        "tenant-b": ["workflow:run"],
    }
    service = AthenaWebService(agent_factory=lambda: object(), session_ttl_seconds=60)
    return TestClient(create_app(settings=settings, service=service))


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _wait_for_task(client: TestClient, task_id: str, key: str) -> dict[str, object]:
    for _ in range(50):
        response = client.get(f"/api/tasks/{task_id}", headers=_headers(key))
        assert response.status_code == 200
        data = response.json()["data"]
        if data["status"] in {"success", "failed"}:
            return data
        time.sleep(0.02)
    raise AssertionError("compatibility task did not finish")


def test_workflow_status_hides_cross_tenant_records() -> None:
    with _client() as client:
        created = client.post(
            "/api/workflow/run",
            headers=_headers("key-a"),
            json={"task": "collect logs; validate"},
        )
        assert created.status_code == 200
        task_id = created.json()["task_id"]

        assert (
            client.get(
                f"/api/workflow/{task_id}/status", headers=_headers("key-a")
            ).status_code
            == 200
        )
        cross_tenant = client.get(
            f"/api/workflow/{task_id}/status", headers=_headers("key-b")
        )
        missing = client.get(
            "/api/workflow/not-a-real-task/status", headers=_headers("key-b")
        )

    assert cross_tenant.status_code == missing.status_code == 404
    assert cross_tenant.json()["error_code"] == missing.json()["error_code"] == "TASK_NOT_FOUND"


def test_async_workflow_compatibility_keeps_inner_task_in_request_tenant() -> None:
    with _client() as client:
        submitted = client.post(
            "/api/tasks",
            headers=_headers("key-a"),
            json={"kind": "workflow", "task": "collect logs; validate"},
        )
        assert submitted.status_code == 200
        outer_task_id = submitted.json()["data"]["task_id"]

        cross_outer = client.get(
            f"/api/tasks/{outer_task_id}", headers=_headers("key-b")
        )
        assert cross_outer.status_code == 404

        finished = _wait_for_task(client, outer_task_id, "key-a")
        assert finished["status"] == "success"
        result = finished["result"]
        assert isinstance(result, dict)
        inner_task_id = result["task_id"]

        assert (
            client.get(
                f"/api/workflow/{inner_task_id}/status", headers=_headers("key-a")
            ).status_code
            == 200
        )
        cross_inner = client.get(
            f"/api/workflow/{inner_task_id}/status", headers=_headers("key-b")
        )
        missing_inner = client.get(
            "/api/workflow/not-a-real-inner-task/status", headers=_headers("key-b")
        )

    assert cross_inner.status_code == missing_inner.status_code == 404
    assert cross_inner.json()["error_code"] == missing_inner.json()["error_code"] == "TASK_NOT_FOUND"
