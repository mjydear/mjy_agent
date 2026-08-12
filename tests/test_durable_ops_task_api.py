"""PostgreSQL-backed OpsTask API compatibility tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.config import AthenaSettings, DatabaseSettings


def test_ops_task_api_uses_durable_repository_when_database_is_configured() -> None:
    app = create_app(
        settings=AthenaSettings(
            database=DatabaseSettings(
                url="sqlite+aiosqlite:///:memory:", auto_migrate=True
            )
        )
    )
    payload = {
        "objective": "diagnose payment CrashLoopBackOff",
        "environment_id": "env-payment",
        "namespace": "payment",
    }
    with TestClient(app) as client:
        first = client.post(
            "/api/ops/tasks",
            headers={"Idempotency-Key": "durable-create"},
            json=payload,
        )
        replay = client.post(
            "/api/ops/tasks",
            headers={"Idempotency-Key": "durable-create"},
            json=payload,
        )
        assert first.status_code == 200
        assert replay.status_code == 200
        task = first.json()["data"]
        assert task["status"] == "queued"
        assert replay.json()["data"]["id"] == task["id"]
        detail = client.get(f"/api/ops/tasks/{task['id']}")
        assert detail.status_code == 200
        assert detail.json()["data"]["checkpoint_version"] == 0
        events = client.get(f"/api/ops/tasks/{task['id']}/events?follow=false")
        assert events.status_code == 200
        assert "event: task.created" in events.text
        listed = client.get("/api/ops/tasks")
        assert listed.status_code == 200
        assert listed.json()["data"]["items"][0]["id"] == task["id"]
        cancelled = client.post(
            f"/api/ops/tasks/{task['id']}/cancel",
            headers={"Idempotency-Key": "durable-cancel"},
        )
        cancelled_replay = client.post(
            f"/api/ops/tasks/{task['id']}/cancel",
            headers={"Idempotency-Key": "durable-cancel"},
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["status"] == "cancelled"
        assert cancelled_replay.status_code == 200
        assert cancelled_replay.json()["data"] == cancelled.json()["data"]
