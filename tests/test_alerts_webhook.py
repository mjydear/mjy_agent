"""告警 webhook 接入路由测试。

# 测试告警 webhook 功能与审计链间的交互，确保报警数据能够被准确记录与处理。

# 测试告警 webhook 功能与审计链间的交互，确保报警数据能够被准确记录与处理。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings


def _service() -> AthenaWebService:
    service = AthenaWebService(agent_factory=lambda: object(), session_ttl_seconds=60)
    return service


def _client(settings: AthenaSettings | None = None) -> TestClient:
    service = _service()
    return TestClient(
        create_app(settings=settings or AthenaSettings(), service=service)
    )


def test_alert_webhook_accepts_alertmanager_payload_real() -> None: # 测试告警 webhook 接收 Alertmanager 负载
    client = _client()
    payload = {
        "alerts": [
            {
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "namespace": "prod",
                    "pod": "checkout-5f8b",
                    "deployment": "checkout",
                    "severity": "critical",
                },
                "annotations": {
                    "summary": "checkout 持续重启",
                    "description": "restart count too high",
                },
            }
        ]
    }
    resp = client.post("/api/alerts/webhook", json=payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"
    assert body["alert_name"] == "KubePodCrashLooping"
    assert body["severity"] == "critical"
    assert body["namespace"] == "prod"
    assert body["pod"] == "checkout-5f8b"
    assert body["deployment"] == "checkout"
    assert body["summary"] == "checkout 持续重启"
    assert body["description"] == "restart count too high"
    assert body["playbook"] == "CrashLoopBackOff"
    assert body["diagnosis_task"] == "诊断 prod 命名空间告警 KubePodCrashLooping"
    assert body["readonly_report"]["namespace"] == "prod"
    assert body["readonly_report"]["findings"]
    assert "prometheus_metrics" in body["readonly_report"]["metrics"]
    assert body["workflow"]["status"] == "success"

    history = client.get("/api/alerts/history")
    assert history.status_code == 200
    assert history.json()["items"][0]["alert_name"] == "KubePodCrashLooping"


def test_alert_webhook_tolerates_minimal_payload() -> None:
    client = _client()
    resp = client.post("/api/alerts/webhook", json={"alert_name": "X"})
    assert resp.status_code == 200
    assert resp.json()["alert_name"] == "X"
    assert resp.json()["namespace"] == "default"
    assert resp.json()["readonly_report"]["namespace"] == "default"


def _settings_with_secret(secret: str = "s3cr") -> AthenaSettings:
    settings = AthenaSettings()
    settings.ops.security.webhook_secret = secret
    return settings


def test_webhook_rejects_missing_secret_when_configured() -> None:
    client = _client(_settings_with_secret())
    resp = client.post("/api/alerts/webhook", json={"alert_name": "X"})
    assert resp.status_code == 401


def test_webhook_accepts_valid_secret() -> None:
    client = _client(_settings_with_secret())
    resp = client.post(
        "/api/alerts/webhook",
        json={"alert_name": "X"},
        headers={"X-Alert-Secret": "s3cr"},
    )
    assert resp.status_code == 200
    assert resp.json()["alert_name"] == "X"


def test_webhook_rejects_wrong_secret() -> None:
    client = _client(_settings_with_secret())
    resp = client.post(
        "/api/alerts/webhook",
        json={"alert_name": "X"},
        headers={"X-Alert-Secret": "wrong"},
    )
    assert resp.status_code == 401


def test_alert_webhook_records_source_and_processing_in_audit_chain() -> None: # 测试告警 webhook 在审计链中记录来源和处理过程
    service = _service()
    client = TestClient(create_app(settings=AthenaSettings(), service=service))

    resp = client.post("/api/alerts/webhook", json={"alert_name": "KubePodCrashLooping"})

    assert resp.status_code == 200
    actions = {event["action"] for event in service.list_audit_events(limit=10)}
    assert "alert.received" in actions
    assert "alert.processed" in actions
