from __future__ import annotations

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.config import (
    AthenaSettings,
    DatabaseSettings,
    OpsSecuritySettings,
    OpsSettings,
    SecuritySettings,
)


def _app(*, write_enabled: bool = False) -> object:
    return create_app(
        settings=AthenaSettings(
            database=DatabaseSettings(
                url="sqlite+aiosqlite:///:memory:", auto_migrate=True
            ),
            ops=OpsSettings(
                security=OpsSecuritySettings(default_readonly=not write_enabled)
            ),
            security=SecuritySettings(
                require_auth=True,
                api_keys={
                    "planner": "tenant-a",
                    "approver": "tenant-a",
                    "viewer": "tenant-b",
                },
                roles={
                    "tenant-a": [
                        "plan:create",
                        "plan:read",
                        "plan:request",
                        "approval:read",
                        "approval:approve",
                        "cloud:execute",
                    ],
                    "tenant-b": [
                        "plan:create",
                        "plan:read",
                        "plan:request",
                        "approval:read",
                        "approval:approve",
                    ],
                },
            ),
        )
    )


def _plan_payload() -> dict[str, object]:
    return {
        "task_id": "ops-1",
        "environment_id": "env-prod",
        "action_type": "scale_deployment",
        "resource_kind": "Deployment",
        "resource_name": "payments-api",
        "namespace": "payments",
        "risk_level": "S3",
        "required_scope": "cloud:execute",
        "parameters": {"replicas": 3},
        "preconditions": {"available_replicas": 2},
        "postconditions": {"available_replicas": 3},
        "rollback": {"command": "kubectl scale deployment/payments-api --replicas=2"},
        "dry_run": {"success": True, "server_side": True},
    }


def test_operation_plan_hash_is_stable_and_tenant_scoped() -> None:
    with TestClient(_app()) as client:
        created = client.post(
            "/api/operation-plans",
            headers={"X-API-Key": "planner"},
            json=_plan_payload(),
        )
        assert created.status_code == 201
        plan = created.json()["data"]
        assert plan["status"] == "draft"
        assert len(plan["plan_hash"]) == 64

        replay = client.post(
            "/api/operation-plans",
            headers={"X-API-Key": "planner"},
            json={
                **_plan_payload(),
                "parameters": {"replicas": 3},
                "dry_run": {"server_side": True, "success": True},
            },
        )
        assert replay.status_code == 201
        assert replay.json()["data"]["id"] == plan["id"]
        assert replay.json()["data"]["plan_hash"] == plan["plan_hash"]
        assert replay.json()["data"]["replayed"] is True

        hidden = client.get(
            f"/api/operation-plans/{plan['id']}",
            headers={"X-API-Key": "viewer"},
        )
        assert hidden.status_code == 404


def test_approval_requires_plan_hash_and_target_scope() -> None:
    with TestClient(_app()) as client:
        plan = client.post(
            "/api/operation-plans",
            headers={"X-API-Key": "planner"},
            json=_plan_payload(),
        ).json()["data"]
        approval = client.post(
            f"/api/operation-plans/{plan['id']}/request-approval",
            headers={"X-API-Key": "planner"},
        ).json()["data"]
        assert approval["status"] == "pending"
        assert (
            client.post(
                f"/api/approvals/{approval['id']}/approve",
                headers={"X-API-Key": "approver"},
                json={"plan_hash": "0" * 64},
            ).status_code
            == 409
        )

        approved = client.post(
            f"/api/approvals/{approval['id']}/approve",
            headers={"X-API-Key": "approver"},
            json={"plan_hash": plan["plan_hash"], "note": "bounded S3 change"},
        )
        assert approved.status_code == 200
        assert approved.json()["data"]["approval"]["status"] == "approved"
        assert approved.json()["data"]["plan"]["status"] == "approved"

        again = client.post(
            f"/api/approvals/{approval['id']}/approve",
            headers={"X-API-Key": "approver"},
            json={"plan_hash": plan["plan_hash"]},
        )
        assert again.status_code == 409


def test_approval_scope_denied_before_execution_scope() -> None:
    with TestClient(_app()) as client:
        plan = client.post(
            "/api/operation-plans",
            headers={"X-API-Key": "viewer"},
            json=_plan_payload(),
        ).json()["data"]
        approval = client.post(
            f"/api/operation-plans/{plan['id']}/request-approval",
            headers={"X-API-Key": "viewer"},
        ).json()["data"]
        denied = client.post(
            f"/api/approvals/{approval['id']}/approve",
            headers={"X-API-Key": "viewer"},
            json={"plan_hash": plan["plan_hash"]},
        )
        assert denied.status_code == 403


def test_reject_moves_plan_and_approval_to_rejected() -> None:
    with TestClient(_app()) as client:
        plan = client.post(
            "/api/operation-plans",
            headers={"X-API-Key": "planner"},
            json={**_plan_payload(), "parameters": {"replicas": 4}},
        ).json()["data"]
        approval = client.post(
            f"/api/operation-plans/{plan['id']}/request-approval",
            headers={"X-API-Key": "planner"},
        ).json()["data"]
        rejected = client.post(
            f"/api/approvals/{approval['id']}/reject",
            headers={"X-API-Key": "approver"},
            json={"note": "change window closed"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["data"]["approval"]["status"] == "rejected"
        assert rejected.json()["data"]["plan"]["status"] == "rejected"


def test_approved_plan_execution_is_disabled_by_default() -> None:
    with TestClient(_app()) as client:
        plan = client.post(
            "/api/operation-plans",
            headers={"X-API-Key": "planner"},
            json=_plan_payload(),
        ).json()["data"]
        approval = client.post(
            f"/api/operation-plans/{plan['id']}/request-approval",
            headers={"X-API-Key": "planner"},
        ).json()["data"]
        client.post(
            f"/api/approvals/{approval['id']}/approve",
            headers={"X-API-Key": "approver"},
            json={"plan_hash": plan["plan_hash"]},
        )

        blocked = client.post(
            f"/api/operation-plans/{plan['id']}/execute",
            headers={"X-API-Key": "approver", "Idempotency-Key": "write-1"},
            json={"approval_id": approval["id"], "plan_hash": plan["plan_hash"]},
        )
        assert blocked.status_code == 403
        assert blocked.json()["error_code"] == "WRITE_EXECUTION_DISABLED"


def test_approved_plan_executes_once_and_replays_tool_effect() -> None:
    with TestClient(_app(write_enabled=True)) as client:
        plan = client.post(
            "/api/operation-plans",
            headers={"X-API-Key": "planner"},
            json=_plan_payload(),
        ).json()["data"]
        approval = client.post(
            f"/api/operation-plans/{plan['id']}/request-approval",
            headers={"X-API-Key": "planner"},
        ).json()["data"]
        client.post(
            f"/api/approvals/{approval['id']}/approve",
            headers={"X-API-Key": "approver"},
            json={"plan_hash": plan["plan_hash"]},
        )

        executed = client.post(
            f"/api/operation-plans/{plan['id']}/execute",
            headers={"X-API-Key": "approver", "Idempotency-Key": "write-1"},
            json={"approval_id": approval["id"], "plan_hash": plan["plan_hash"]},
        )
        assert executed.status_code == 200
        result = executed.json()["data"]
        assert result["replayed"] is False
        assert result["effect"]["status"] == "succeeded"
        assert result["effect"]["plan_hash"] == plan["plan_hash"]
        assert result["plan"]["status"] == "executed"
        assert result["result"]["success"] is True

        replay = client.post(
            f"/api/operation-plans/{plan['id']}/execute",
            headers={"X-API-Key": "approver", "Idempotency-Key": "write-1"},
            json={"approval_id": approval["id"], "plan_hash": plan["plan_hash"]},
        )
        assert replay.status_code == 200
        assert replay.json()["data"]["replayed"] is True
        assert (
            replay.json()["data"]["effect"]["effect_id"]
            == result["effect"]["effect_id"]
        )
