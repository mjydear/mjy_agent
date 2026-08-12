"""Regression coverage for the durable diagnostic learning HTTP seams."""

from __future__ import annotations

import asyncio
import importlib

from fastapi.testclient import TestClient

from athena.api.repositories import TaskCreate
from athena.api.services import AthenaWebService
from athena.config import AthenaSettings, DatabaseSettings, EvidenceSettings


def _service() -> AthenaWebService:
    return AthenaWebService(agent_factory=lambda: object(), session_ttl_seconds=60)


def _settings(tmp_path) -> AthenaSettings:
    return AthenaSettings(
        database=DatabaseSettings(
            url="sqlite+aiosqlite:///:memory:", auto_migrate=True
        ),
        evidence=EvidenceSettings(local_root=str(tmp_path / "evidence")),
    )


def _seed_verified_case(app) -> tuple[str, str]:
    task = TaskCreate(
        task_id="task-learning-api-1",
        tenant_id="public",
        objective="diagnose a pending payment Pod",
        environment_id="env-payment",
        environment_mode="mock",
        scope={"namespace": "payment"},
        policy_snapshot={"readonly": True, "version": "policy-v1"},
        config_snapshot={"model": "rules-only", "tool_set": "k8s-readonly-v1"},
        budget={"remaining_steps": 4, "remaining_tokens": 6000},
        execution_profile="bounded_policy_loop",
        workflow_type="pod_pending",
    )
    asyncio.run(app.state.task_repository.create_task(task))
    evidence = asyncio.run(
        app.state.durable_evidence_repository.create(
            tenant_id="public",
            task_id=task.task_id,
            evidence_type="kubernetes_event",
            source="k8s.events.list",
            data_origin="mock",
            summary="Pod is unschedulable because no node has enough memory.",
            content={"reason": "Insufficient memory"},
        )
    )
    return task.task_id, evidence.evidence_id


def _finalize_and_confirm_case(
    client: TestClient, task_id: str, evidence_id: str
) -> tuple[str, str]:
    outcome_response = client.post(
        f"/api/diagnosis-outcomes/tasks/{task_id}/finalize",
        json={
            "root_cause": "No eligible node has sufficient allocatable memory.",
            "supporting_evidence_ids": [evidence_id],
            "remediation_recommendation": "Review resource requests before an approved change.",
            "confidence": 0.91,
            "evidence_sufficient": True,
        },
    )
    assert outcome_response.status_code == 201
    outcome = outcome_response.json()["data"]
    assert outcome["supporting_evidence_ids"] == [evidence_id]

    feedback_response = client.post(
        f"/api/diagnosis-outcomes/{outcome['id']}/feedback",
        headers={"Idempotency-Key": "feedback-learning-api-1"},
        json={
            "task_id": task_id,
            "feedback_type": "confirmed",
            "note": "The operator verified the scheduling diagnosis.",
            "recovery_observed_at": "2026-08-09T10:15:00Z",
            "recovery_summary": "The Pod remained ready after capacity was restored.",
        },
    )
    assert feedback_response.status_code == 201
    feedback = feedback_response.json()["data"]
    assert feedback["recovery"]["summary"] == (
        "The Pod remained ready after capacity was restored."
    )
    return outcome["id"], feedback["id"]


def test_server_import_mounts_learning_routes_and_wires_durable_services(tmp_path) -> None:
    server = importlib.import_module("athena.api.server")
    app = server.create_app(settings=_settings(tmp_path), service=_service())
    try:
        routes = {route.path for route in app.routes}
        assert "/api/diagnosis-outcomes/tasks/{task_id}/finalize" in routes
        assert "/api/diagnosis-outcomes/{outcome_id}/feedback" in routes
        assert "/api/skill-candidates" in routes
        assert "/api/skill-candidates/{candidate_id}/bridge" in routes
        assert app.state.diagnosis_outcome_service is not None
        assert app.state.operator_feedback_service is not None
        assert app.state.skill_candidate_service is not None
    finally:
        asyncio.run(app.state.database.dispose())


def test_outcome_feedback_and_skill_candidate_apis_keep_activation_human_gated(
    tmp_path,
) -> None:
    from athena.api.server import create_app

    app = create_app(settings=_settings(tmp_path), service=_service())
    with TestClient(app) as client:
        task_id, evidence_id = _seed_verified_case(app)
        outcome_id, feedback_id = _finalize_and_confirm_case(
            client, task_id, evidence_id
        )

        candidate_response = client.post(
            "/api/skill-candidates",
            json={
                "name": "pending-pod-capacity-triage",
                "workflow_type": "pod_pending",
                "environment_type": "kubernetes",
                "capabilities": ["k8s.events.read", "k8s.workload.read"],
                "outcome_id": outcome_id,
                "feedback_id": feedback_id,
                "evidence_ids": [evidence_id],
            },
        )
        assert candidate_response.status_code == 201
        candidate = candidate_response.json()["data"]
        assert candidate["status"] == "candidate"
        assert candidate["online_eligible"] is False

        pending_response = client.post(
            f"/api/skill-candidates/{candidate['id']}/replay-pending"
        )
        assert pending_response.status_code == 200
        assert pending_response.json()["data"]["status"] == "replay_pending"

        replay_response = client.post(
            f"/api/skill-candidates/{candidate['id']}/replay",
            json={"report_id": "replay-learning-api-1", "passed": True},
        )
        assert replay_response.status_code == 200
        assert replay_response.json()["data"]["status"] == "shadow"

        shadow_response = client.post(
            f"/api/skill-candidates/{candidate['id']}/shadow",
            json={"report_id": "shadow-learning-api-1", "passed": True},
        )
        assert shadow_response.status_code == 200
        assert shadow_response.json()["data"]["status"] == "review_pending"

        bridge_response = client.get(
            f"/api/skill-candidates/{candidate['id']}/bridge"
        )
        assert bridge_response.status_code == 200
        assert bridge_response.json()["data"]["activation_allowed"] is False
