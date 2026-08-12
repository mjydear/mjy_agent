"""Durable Alertmanager ingress contract tests."""

from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.config import AthenaSettings, DatabaseSettings, RuntimeSettings


def test_alert_webhook_returns_durable_202_and_replays_duplicate_receipt() -> None:
    settings = AthenaSettings(
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:", auto_migrate=True)
    )
    app = create_app(settings=settings)
    payload = {
        "alerts": [
            {
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "namespace": "payment",
                    "pod": "api-0",
                    "severity": "critical",
                },
                "annotations": {"summary": "pod restarts"},
            }
        ]
    }
    traceparent = "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"
    with TestClient(app) as client:
        first = client.post(
            "/api/alerts/webhook", json=payload, headers={"traceparent": traceparent}
        )
        duplicate = client.post(
            "/api/alerts/webhook", json=payload, headers={"traceparent": traceparent}
        )
        assert first.status_code == 202
        assert duplicate.status_code == 202
        first_data = first.json()
        duplicate_data = duplicate.json()
        assert first_data["status"] == "accepted"
        assert first_data["created"] is True
        assert first.headers["traceparent"] == traceparent
        assert duplicate_data["duplicate"] is True
        assert duplicate_data["task_id"] == first_data["task_id"]
        messages = asyncio.run(
            app.state.outbox_repository.claim_batch("test-relay", limit=10)
        )
        assert len(messages) == 1
        assert messages[0].payload["task_id"] == first_data["task_id"]
        assert messages[0].traceparent == traceparent


def test_durable_alert_batch_creates_one_task_per_fingerprint_and_replays() -> None:
    settings = AthenaSettings(
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:", auto_migrate=True)
    )
    app = create_app(settings=settings)
    payload = {
        "alerts": [
            {
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "namespace": "payment",
                    "pod": "api-0",
                    "severity": "critical",
                    "api_token": "secret-token",
                },
                "annotations": {"summary": "pod restarts"},
            },
            {
                "labels": {
                    "alertname": "KubePodPending",
                    "namespace": "payment",
                    "pod": "worker-0",
                    "severity": "warning",
                },
                "annotations": {"authorization": "Bearer secret"},
            },
        ]
    }

    with TestClient(app) as client:
        first = client.post("/api/alerts/webhook", json=payload)
        duplicate = client.post("/api/alerts/webhook", json=payload)

        assert first.status_code == 202
        assert duplicate.status_code == 202
        first_data = first.json()
        duplicate_data = duplicate.json()
        assert first_data["processed_count"] == 2
        assert duplicate_data["processed_count"] == 2
        assert {item["alert_name"] for item in first_data["alerts"]} == {
            "KubePodCrashLooping",
            "KubePodPending",
        }
        assert {item["created"] for item in first_data["alerts"]} == {True}
        assert {item["duplicate"] for item in duplicate_data["alerts"]} == {True}
        assert {item["task_id"] for item in first_data["alerts"]} == {
            item["task_id"] for item in duplicate_data["alerts"]
        }

        messages = asyncio.run(
            app.state.outbox_repository.claim_batch("batch-relay", limit=10)
        )
        assert len(messages) == 2
        assert {message.payload["task_id"] for message in messages} == {
            item["task_id"] for item in first_data["alerts"]
        }

        receipts = asyncio.run(
            app.state.task_repository.list_alert_receipts("public", limit=10)
        )
        assert len(receipts) == 2
        receipt_payloads = [item["payload"] for item in receipts]
        assert all("secret-token" not in str(payload) for payload in receipt_payloads)
        assert all("Bearer secret" not in str(payload) for payload in receipt_payloads)


def test_durable_alert_requires_machine_token_when_auth_is_enabled() -> None:
    settings = AthenaSettings(
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:", auto_migrate=True)
    )
    settings.security.require_auth = True
    settings.security.api_keys = {"user-key": "tenant-a"}
    settings.security.roles = {"tenant-a": ["workflow:run"]}
    settings.security.alert_integration_tokens = {"alert-token": "tenant-a"}
    app = create_app(settings=settings)
    payload = {
        "alerts": [
            {
                "labels": {
                    "alertname": "KubePodCrashLooping",
                    "namespace": "payment",
                    "pod": "api-0",
                    "severity": "critical",
                }
            }
        ]
    }

    with TestClient(app) as client:
        missing = client.post("/api/alerts/webhook", json=payload)
        invalid = client.post(
            "/api/alerts/webhook",
            json=payload,
            headers={"X-Alert-Token": "bad-token"},
        )
        interactive_without_scope = client.post(
            "/api/alerts/webhook",
            json=payload,
            headers={"X-API-Key": "user-key"},
        )
        accepted = client.post(
            "/api/alerts/webhook",
            json=payload,
            headers={
                "X-Alert-Token": "alert-token",
                "X-Alert-Integration": "alertmanager-prod",
            },
        )

    assert missing.status_code == 401
    assert invalid.status_code == 401
    assert interactive_without_scope.status_code == 403
    assert accepted.status_code == 202
    assert accepted.json()["status"] == "accepted"


def test_production_alert_webhook_fails_closed_without_integration_token() -> None:
    settings = AthenaSettings(
        runtime=RuntimeSettings(profile="production"),
        database=DatabaseSettings(
            url="sqlite+aiosqlite:///:memory:", auto_migrate=True
        ),
    )
    app = create_app(settings=settings)

    with TestClient(app) as client:
        response = client.post(
            "/api/alerts/webhook",
            json={
                "alerts": [
                    {
                        "labels": {
                            "alertname": "KubePodCrashLooping",
                            "namespace": "payment",
                        }
                    }
                ]
            },
        )

    assert response.status_code == 401
    assert response.json()["error_code"] == "ALERT_INTEGRATION_UNAUTHORIZED"
