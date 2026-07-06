"""告警 webhook 接入路由测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings


def _client() -> TestClient:
    service = AthenaWebService(agent_factory=lambda: object(), session_ttl_seconds=60)
    return TestClient(create_app(settings=AthenaSettings(), service=service))


def test_alert_webhook_accepts_alertmanager_payload() -> None:
    client = _client()
    payload = {
        "alerts": [
            {
                "labels": {"alertname": "AthenaHighErrorRate", "severity": "critical"},
                "annotations": {"summary": "5xx 过高"},
            }
        ]
    }
    resp = client.post("/api/alerts/webhook", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "accepted"
    assert body["alert_name"] == "AthenaHighErrorRate"
    assert body["severity"] == "critical"


def test_alert_webhook_tolerates_minimal_payload() -> None:
    client = _client()
    resp = client.post("/api/alerts/webhook", json={"alert_name": "X"})
    assert resp.status_code == 200
    assert resp.json()["alert_name"] == "X"
