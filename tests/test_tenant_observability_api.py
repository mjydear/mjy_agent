"""Tenant isolation regression coverage for trace, audit, and benchmark reads."""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings


def _client() -> TestClient:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {
        "key-a": "tenant-a",
        "key-b": "tenant-b",
        "key-auditor": "auditor",
    }
    settings.security.roles = {
        "tenant-a": ["workflow:run", "benchmark:run", "audit:read"],
        "tenant-b": ["workflow:run", "benchmark:run", "audit:read"],
        "auditor": ["audit:read", "audit:read:any"],
    }
    service = AthenaWebService(agent_factory=lambda: object(), session_ttl_seconds=60)
    return TestClient(create_app(settings=settings, service=service))


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def _run_workflow(client: TestClient, key: str) -> str:
    response = client.post(
        "/api/workflow/run",
        headers=_headers(key),
        json={"task": "collect logs; validate"},
    )
    assert response.status_code == 200
    return response.json()["task_id"]


def test_trace_benchmark_and_audit_reads_are_tenant_scoped() -> None:
    with _client() as client:
        task_a = _run_workflow(client, "key-a")
        task_b = _run_workflow(client, "key-b")

        own_trace = client.get(f"/api/traces/{task_a}", headers=_headers("key-a"))
        assert own_trace.status_code == 200

        cross_trace = client.get(f"/api/traces/{task_a}", headers=_headers("key-b"))
        missing_trace = client.get(
            "/api/traces/not-a-real-task", headers=_headers("key-b")
        )
        assert cross_trace.status_code == missing_trace.status_code == 404
        assert cross_trace.json()["error_code"] == missing_trace.json()["error_code"]

        benchmark = client.post(
            "/api/benchmark/run",
            headers=_headers("key-a"),
            json={"case_set": "tenant-a"},
        )
        assert benchmark.status_code == 200
        run_id = benchmark.json()["run_id"]
        assert (
            client.get(
                f"/api/benchmark/{run_id}/report", headers=_headers("key-a")
            ).status_code
            == 200
        )
        cross_report = client.get(
            f"/api/benchmark/{run_id}/report", headers=_headers("key-b")
        )
        assert cross_report.status_code == 404
        assert cross_report.json()["error_code"] == "BENCHMARK_NOT_FOUND"

        events_a = client.get("/api/audit/events", headers=_headers("key-a"))
        assert events_a.status_code == 200
        assert events_a.json()["events"]
        assert {event["tenant_id"] for event in events_a.json()["events"]} == {
            "tenant-a"
        }

        events_b = client.get("/api/audit/events", headers=_headers("key-b"))
        assert events_b.status_code == 200
        assert {event["tenant_id"] for event in events_b.json()["events"]} == {
            "tenant-b"
        }

        override = client.get(
            "/api/audit/events?tenant_id=tenant-b", headers=_headers("key-a")
        )
        assert override.status_code == 403
        assert override.json()["error_code"] == "FORBIDDEN"

        audited_events = client.get(
            "/api/audit/events?tenant_id=tenant-b", headers=_headers("key-auditor")
        )
        assert audited_events.status_code == 200
        assert {event["tenant_id"] for event in audited_events.json()["events"]} == {
            "tenant-b"
        }
        assert client.get("/api/audit/verify", headers=_headers("key-a")).status_code == 200
        assert task_b
