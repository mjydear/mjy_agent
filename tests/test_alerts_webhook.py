"""告警 webhook 接入路由测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings, K8sSettings, OpsSettings
from athena.integration.alert_webhook import AlertWebhookParser


def _service() -> AthenaWebService:
    service = AthenaWebService(agent_factory=lambda: object(), session_ttl_seconds=60)
    return service


def _client() -> TestClient:
    service = _service()
    return TestClient(create_app(settings=AthenaSettings(), service=service))


def test_alert_webhook_accepts_alertmanager_payload() -> None:
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
    assert body["data_origin"] == "mock"
    assert body["audit_status"] == "recorded"

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


def test_alert_webhook_records_source_and_processing_in_audit_chain() -> None:
    service = _service()
    client = TestClient(create_app(settings=AthenaSettings(), service=service))

    resp = client.post(
        "/api/alerts/webhook", json={"alert_name": "KubePodCrashLooping"}
    )

    assert resp.status_code == 200
    actions = {event["action"] for event in service.list_audit_events(limit=10)}
    assert "alert.received" in actions
    assert "alert.processed" in actions
    received = next(
        event
        for event in service.list_audit_events(limit=10)
        if event["action"] == "alert.received"
    )
    assert received["success"] is True


def test_alert_webhook_processes_every_alert_in_batch() -> None:
    client = _client()
    response = client.post(
        "/api/alerts/webhook",
        json={
            "alerts": [
                {
                    "labels": {
                        "alertname": "KubePodCrashLooping",
                        "namespace": "default",
                    }
                },
                {"labels": {"alertname": "KubePodPending", "namespace": "default"}},
            ]
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["processed_count"] == 2
    assert {alert["alert_name"] for alert in payload["alerts"]} == {
        "KubePodCrashLooping",
        "KubePodPending",
    }


def test_alert_parser_redacts_sensitive_metadata_and_rejects_missing_name() -> None:
    parser = AlertWebhookParser()
    alert = parser.parse_all(
        {
            "alerts": [
                {
                    "labels": {
                        "alertname": "KubePodCrashLooping",
                        "api_token": "secret",
                    },
                    "annotations": {
                        "summary": "restart",
                        "authorization": "Bearer secret",
                    },
                }
            ]
        }
    )[0]
    assert alert.labels["api_token"] == "[REDACTED]"
    assert alert.annotations["authorization"] == "[REDACTED]"

    response = _client().post("/api/alerts/webhook", json={"alerts": [{}]})
    assert response.status_code == 400
    assert response.json()["error_code"] == "ALERT_PAYLOAD_INVALID"


def test_live_alert_uses_app_config_and_fails_closed_without_mock_history() -> None:
    class ExplodingCoreApi:
        def list_namespaced_pod(self, **kwargs: object) -> object:
            raise ConnectionError("cluster unavailable")

    settings = AthenaSettings(
        ops=OpsSettings(
            mode="real",
            kubernetes=K8sSettings(
                namespace_allowlist=["default"],
                fallback_policy="fail_closed",
            ),
        )
    )
    service = _service()
    app = create_app(settings=settings, service=service)
    app.state.ops_k8s_client._core_api = ExplodingCoreApi()

    with TestClient(app) as client:
        response = client.post(
            "/api/alerts/webhook",
            json={
                "alerts": [
                    {
                        "labels": {
                            "alertname": "KubePodCrashLooping",
                            "namespace": "default",
                        }
                    }
                ]
            },
        )
        history = client.get("/api/alerts/history").json()["items"]

    assert response.status_code == 503
    assert response.json()["error_code"] == "ENV_CONNECTION_FAILED"
    assert app.state.ops_k8s_client.last_data_origin == "unavailable"
    assert history == []
