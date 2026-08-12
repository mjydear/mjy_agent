"""Phase 1 OpsTask API facts and persisted SSE replay."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.config import AthenaSettings, K8sSettings, OpsSettings


def test_ops_task_api_creates_scopes_and_replays_persisted_events() -> None:
    client = TestClient(create_app(settings=AthenaSettings()))

    created = client.post(
        "/api/ops/tasks",
        headers={"Idempotency-Key": "create-default-task"},
        json={
            "objective": "diagnose payment CrashLoopBackOff",
            "environment_id": "env-default",
            "namespace": "default",
        },
    )

    assert created.status_code == 200
    task = created.json()["data"]
    assert task["status"] == "queued"
    task_id = task["id"]
    assert client.get(f"/api/ops/tasks/{task_id}").json()["data"]["id"] == task_id
    stream = client.get(f"/api/ops/tasks/{task_id}/events")
    assert stream.status_code == 200
    assert "id: 1" in stream.text
    assert "event: task.created" in stream.text
    incremental = client.get(
        f"/api/ops/tasks/{task_id}/events", headers={"Last-Event-ID": "1"}
    )
    assert "id: 1\n" not in incremental.text

    deadline = time.monotonic() + 2
    detail = client.get(f"/api/ops/tasks/{task_id}").json()["data"]
    while detail["status"] not in {"succeeded", "failed", "cancelled"}:
        assert time.monotonic() < deadline
        time.sleep(0.01)
        detail = client.get(f"/api/ops/tasks/{task_id}").json()["data"]

    assert detail["status"] == "succeeded"
    assert detail["phase"] == "report"
    evidence = client.get(f"/api/ops/tasks/{task_id}/evidence").json()["data"]["items"]
    assert [item["source"] for item in evidence] == [
        "k8s.pod.list",
        "k8s.pod.get",
        "k8s.events.list",
        "k8s.logs.read",
    ]
    assert {item["data_origin"] for item in evidence} == {"mock"}
    report = client.get(f"/api/ops/tasks/{task_id}/report").json()["data"]
    assert report["root_causes"] == [
        {
            "summary": "Database connection failure reported by the container log",
            "evidence_ids": [evidence[-1]["id"]],
        }
    ]
    assert all(
        spec.readonly for spec in client.app.state.ops_workflow.available_tools()
    )
    terminal_cancel = client.post(
        f"/api/ops/tasks/{task_id}/cancel",
        headers={"Idempotency-Key": "cancel-default-task"},
    )
    assert terminal_cancel.status_code == 200
    assert terminal_cancel.json()["data"]["status"] == "succeeded"
    history = client.get(f"/api/ops/tasks/{task_id}/events?follow=false").text
    assert "event: task.cancelled" not in history


def test_live_task_never_persists_mock_evidence_when_fallback_is_misconfigured() -> (
    None
):
    class ExplodingCoreApi:
        def list_namespaced_pod(self, **kwargs: object) -> object:
            raise ConnectionError("cluster unavailable")

    settings = AthenaSettings(
        ops=OpsSettings(
            mode="real",
            kubernetes=K8sSettings(
                namespace_allowlist=["default"],
                fallback_policy="allow_mock",
            ),
        )
    )
    app = create_app(settings=settings)
    app.state.ops_k8s_client._core_api = ExplodingCoreApi()
    with TestClient(app) as client:
        assert app.state.ops_k8s_client.fallback_policy == "fail_closed"
        assert client.get("/readyz").status_code == 503
        created = client.post(
            "/api/ops/tasks",
            headers={"Idempotency-Key": "create-live-task"},
            json={
                "objective": "diagnose CrashLoopBackOff",
                "environment_id": "env-live",
                "namespace": "default",
            },
        )
        task_id = created.json()["data"]["id"]
        deadline = time.monotonic() + 2
        detail = client.get(f"/api/ops/tasks/{task_id}").json()["data"]
        while detail["status"] not in {"succeeded", "failed", "cancelled"}:
            assert time.monotonic() < deadline
            time.sleep(0.01)
            detail = client.get(f"/api/ops/tasks/{task_id}").json()["data"]

        evidence = client.get(f"/api/ops/tasks/{task_id}/evidence").json()["data"]

    assert detail["status"] == "failed"
    assert evidence["items"] == []
