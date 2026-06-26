"""Smoke tests for CloudOps vertical scenarios."""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from tests.test_web_console import build_test_agent


def build_cloud_client() -> TestClient:
    """Build an isolated Web API client for CloudOps tests."""
    service = AthenaWebService(agent_factory=build_test_agent, session_ttl_seconds=60)
    return TestClient(create_app(service=service))


def test_cloud_ops_modes_and_four_scenarios() -> None:
    client = build_cloud_client()

    modes = client.get("/api/cloud-ops/modes")
    assert modes.status_code == 200
    assert {mode["mode"] for mode in modes.json()} == {
        "k8s",
        "resource",
        "fault",
        "cost",
    }

    for mode in ("k8s", "resource", "fault", "cost"):
        response = client.post(
            "/api/cloud-ops/run", json={"mode": mode, "task": "KubePodCrashLooping"}
        )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "success"
        assert payload["steps"]
        assert payload["answer"]


def test_cloud_ops_high_risk_requires_confirmation() -> None:
    client = build_cloud_client()

    blocked = client.post(
        "/api/cloud-ops/run", json={"mode": "resource", "task": "restart instance"}
    )
    assert blocked.status_code == 200
    assert blocked.json()["requires_confirmation"] is True
    assert blocked.json()["status"] == "waiting_confirmation"

    confirmed = client.post(
        "/api/cloud-ops/run",
        json={"mode": "resource", "task": "restart instance", "confirmed": True},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["requires_confirmation"] is False
    assert confirmed.json()["status"] == "success"


def test_cloud_ops_stream_and_knowledge() -> None:
    client = build_cloud_client()

    with client.stream(
        "POST",
        "/api/cloud-ops/stream",
        json={"mode": "fault", "task": "KubePodCrashLooping"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())
    assert "data:" in body
    assert "Root cause" in body

    knowledge = client.get("/api/cloud-ops/knowledge", params={"query": "CrashLoop"})
    assert knowledge.status_code == 200
    assert knowledge.json()["items"]
