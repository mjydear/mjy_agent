"""Cross-tenant regression coverage for tenant-aware presentation resources."""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings


def _client() -> TestClient:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-a": "tenant-a", "key-b": "tenant-b"}
    service = AthenaWebService(agent_factory=lambda: object(), session_ttl_seconds=60)
    return TestClient(create_app(settings=settings, service=service))


def _headers(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


def test_knowledge_alert_history_and_llm_configs_are_tenant_scoped() -> None:
    with _client() as client:
        document_a = client.post(
            "/api/knowledge/documents",
            headers=_headers("key-a"),
            json={
                "title": "Tenant A Redis Runbook",
                "content": "shared outage wording but tenant A remediation only",
                "tags": ["redis"],
            },
        )
        document_b = client.post(
            "/api/knowledge/documents",
            headers=_headers("key-b"),
            json={
                "title": "Tenant B Redis Runbook",
                "content": "shared outage wording but tenant B remediation only",
                "tags": ["redis"],
            },
        )
        assert document_a.status_code == document_b.status_code == 200
        document_a_id = document_a.json()["data"]["doc_id"]

        listed_a = client.get("/api/knowledge/documents", headers=_headers("key-a"))
        listed_b = client.get("/api/knowledge/documents", headers=_headers("key-b"))
        assert [item["title"] for item in listed_a.json()["data"]] == [
            "Tenant A Redis Runbook"
        ]
        assert [item["title"] for item in listed_b.json()["data"]] == [
            "Tenant B Redis Runbook"
        ]
        assert (
            client.delete(
                f"/api/knowledge/documents/{document_a_id}", headers=_headers("key-b")
            ).status_code
            == 404
        )
        recalled_b = client.post(
            "/api/knowledge/search",
            headers=_headers("key-b"),
            json={"query": "shared outage wording", "top_k": 5},
        )
        assert recalled_b.status_code == 200
        assert {item["title"] for item in recalled_b.json()["data"]} == {
            "Tenant B Redis Runbook"
        }

        alert_a = client.post(
            "/api/alerts/webhook",
            headers=_headers("key-a"),
            json={"alert_name": "TenantAOnly"},
        )
        alert_b = client.post(
            "/api/alerts/webhook",
            headers=_headers("key-b"),
            json={"alert_name": "TenantBOnly"},
        )
        assert alert_a.status_code == alert_b.status_code == 200
        assert [
            item["alert_name"]
            for item in client.get(
                "/api/alerts/history", headers=_headers("key-a")
            ).json()["items"]
        ] == ["TenantAOnly"]
        assert [
            item["alert_name"]
            for item in client.get(
                "/api/alerts/history", headers=_headers("key-b")
            ).json()["items"]
        ] == ["TenantBOnly"]

        config_a = client.post(
            "/api/llm/configs",
            headers=_headers("key-a"),
            json={
                "provider": "deepseek",
                "display_name": "Tenant A model",
                "model": "deepseek/deepseek-chat",
                "api_key": "sk-tenant-a-only",
            },
        )
        assert config_a.status_code == 201
        config_a_id = config_a.json()["config_id"]
        assert client.get("/api/llm/configs", headers=_headers("key-b")).json() == []
        assert (
            client.delete(
                f"/api/llm/configs/{config_a_id}", headers=_headers("key-b")
            ).status_code
            == 404
        )
