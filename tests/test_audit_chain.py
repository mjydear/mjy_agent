"""审计哈希链存储 + 查询/校验 API 测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings
from athena.infra.cache import InMemoryCache
from athena.tools.audit_chain import HashChainAuditStore


def _store() -> HashChainAuditStore:
    return HashChainAuditStore(InMemoryCache(namespace="audit-test"))


def test_append_links_prev_hash_and_verifies() -> None:
    store = _store()
    r1 = store.append(actor="a", action="x.run", resource="t1", success=True)
    r2 = store.append(actor="b", action="y.run", resource="t2", success=False)
    assert r1.seq == 1 and r2.seq == 2
    assert r2.prev_hash == r1.hash
    result = store.verify_chain()
    assert result["valid"] is True
    assert result["checked"] == 2


def test_tamper_detected() -> None:
    cache = InMemoryCache(namespace="audit-tamper")
    store = HashChainAuditStore(cache)
    store.append(actor="a", action="x.run", resource="t1", success=True)
    store.append(actor="b", action="y.run", resource="t2", success=True)
    # 篡改第一条记录的 detail，但不重算哈希 → 校验应失败
    import json

    raw = json.loads(cache.get("audit:system:1"))
    raw["detail"] = "tampered"
    cache.set("audit:system:1", json.dumps(raw))
    result = store.verify_chain()
    assert result["valid"] is False
    assert result["broken_at"] == 1


def test_list_filters_by_tenant() -> None:
    store = _store()
    store.append(actor="team-a", action="x", resource="1", success=True, tenant_id="team-a")
    store.append(actor="team-b", action="x", resource="2", success=True, tenant_id="team-b")
    events = store.list(limit=10, tenant_id="team-a")
    assert len(events) == 1
    assert events[0].tenant_id == "team-a"


def _client(settings: AthenaSettings) -> TestClient:
    service = AthenaWebService(agent_factory=lambda: object(), session_ttl_seconds=60)
    return TestClient(create_app(settings=settings, service=service))


def test_audit_api_records_workflow_and_verifies() -> None:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-admin": "admin"}
    settings.security.roles = {"admin": ["workflow:run", "audit:read"]}
    client = _client(settings)
    headers = {"X-API-Key": "key-admin"}

    run = client.post("/api/workflow/run", json={"task": "巡检"}, headers=headers)
    assert run.status_code == 200

    events = client.get("/api/audit/events", headers=headers)
    assert events.status_code == 200
    body = events.json()
    assert body["count"] >= 1
    assert any(e["action"] == "workflow.run" for e in body["events"])

    verify = client.get("/api/audit/verify", headers=headers)
    assert verify.status_code == 200
    assert verify.json()["valid"] is True


def test_audit_api_requires_scope() -> None:
    settings = AthenaSettings()
    settings.security.require_auth = True
    settings.security.api_keys = {"key-view": "viewer"}
    settings.security.roles = {"viewer": []}
    client = _client(settings)
    resp = client.get("/api/audit/events", headers={"X-API-Key": "key-view"})
    assert resp.status_code == 403
