"""HTTP acceptance test for the human-gated Runtime learning lifecycle."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from athena.api.server import create_app
from athena.config import AthenaSettings, DatabaseSettings


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_runtime_learning_requires_feedback_replay_shadow_and_review() -> None:
    repository = Path(__file__).parent / "fixtures" / "runtime_repo"
    settings = AthenaSettings(
        database=DatabaseSettings(
            url="sqlite+aiosqlite:///:memory:", auto_migrate=True
        )
    )
    with TestClient(create_app(settings=settings)) as client:
        task = _data(
            client.post(
                "/api/runtime/tasks",
                json={
                    "goal": "诊断价格计算失败",
                    "repository_path": str(repository),
                    "profile": "standard",
                },
            )
        )
        task_id = task["id"]
        detail = _data(client.post(f"/api/runtime/tasks/{task_id}/run"))
        evidence_ids = detail["report"]["evidence_ids"]
        assert len(evidence_ids) == 3

        observed = _data(
            client.post(
                f"/api/runtime/skills/tasks/{task_id}/feedback",
                json={
                    "feedback_id": "feedback-api-1",
                    "accepted": True,
                    "verified": True,
                    "summary": "人工确认该只读诊断建议有效。",
                    "submitted_by": "operator-a",
                },
            )
        )
        candidate = observed["candidate"]
        assert candidate["status"] == "candidate"
        candidate_id = candidate["id"]
        root_cause = candidate["procedure"]["root_cause"]
        trajectory = observed["trajectory"]
        assert trajectory["status"] == "eligible"
        assert trajectory["admission"]["eligible"] is True

        persisted_trajectory = _data(
            client.get(
                f"/api/runtime/skills/trajectories/{trajectory['trajectory_id']}"
            )
        )
        assert persisted_trajectory["trajectory_id"] == trajectory["trajectory_id"]
        assert [item["to_status"] for item in persisted_trajectory["events"]] == [
            "observed",
            "eligible",
        ]

        structured_candidate = client.post(
            "/api/skill-candidates/from-trajectories",
            json={
                "name": "pricing-diagnosis-candidate",
                "description": "Diagnose repeatable pricing failures from Evidence.",
                "trigger": {"task_type": "repository_diagnosis"},
                "allowed_tools": ["search_code", "read_file_range", "run_test"],
                "procedure": [
                    "Search for the calculation entry point.",
                    "Read bounded source and collect Evidence.",
                    "Run the allowlisted repository check.",
                ],
                "failure_recovery": [
                    "Stop after a rejected tool call and request human review."
                ],
                "success_contract": {
                    "requires_root_cause": True,
                    "requires_evidence": True,
                },
                "evidence_requirements": [
                    "Evidence must directly support the reported root cause."
                ],
                "token_budget_hint": 8000,
                "source_trajectory_ids": [trajectory["trajectory_id"]],
                "version": 1,
                "risk_level": "S1",
            },
        )
        assert structured_candidate.status_code == 201
        structured = structured_candidate.json()["data"]
        assert structured["status"] == "candidate"
        assert structured["evaluation_status"] == "not_evaluated"
        assert structured["online_eligible"] is False
        assert structured["manifest"]["activation_allowed"] is False

        validation = client.post(
            f"/api/skill-candidates/{structured['id']}/validate"
        )
        assert validation.status_code == 200, validation.text
        validation_data = validation.json()["data"]
        assert validation_data["passed"] is True
        assert validation_data["schema_valid"] is True
        assert validation_data["security_valid"] is True
        assert validation_data["activation_allowed"] is False

        persisted_validation = client.get(
            "/api/skill-candidates/validations/"
            f"{validation_data['report_id']}"
        )
        assert persisted_validation.status_code == 200
        assert persisted_validation.json()["data"]["report_id"] == validation_data[
            "report_id"
        ]

        replay = _data(
            client.post(
                f"/api/runtime/skills/{candidate_id}/replay",
                json={
                    "cases": [
                        {
                            "case_id": "api-replay-1",
                            "expected_root_cause": root_cause,
                            "required_evidence_ids": evidence_ids,
                        }
                    ]
                },
            )
        )
        assert replay["candidate"]["status"] == "shadow"

        shadow = _data(
            client.post(
                f"/api/runtime/skills/{candidate_id}/shadow",
                json={
                    "cases": [
                        {
                            "case_id": "api-shadow-1",
                            "observed_root_cause": root_cause,
                            "observed_evidence_ids": evidence_ids[:1],
                            "effect_count": 0,
                        }
                    ]
                },
            )
        )
        assert shadow["candidate"]["status"] == "review_pending"

        reviewed = _data(
            client.post(
                f"/api/runtime/skills/{candidate_id}/review",
                json={
                    "reviewer": "lead-a",
                    "approved": True,
                    "note": "只读能力和来源已审核。",
                },
            )
        )
        assert reviewed["candidate"]["handoff_ready"] is True

        handoff = _data(client.post(f"/api/runtime/skills/{candidate_id}/handoff"))
        assert handoff["activation_allowed"] is False
        assert handoff["requires_manual_draft_creation"] is True
