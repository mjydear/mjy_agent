"""HTTP acceptance test for the human-gated Runtime learning lifecycle."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from athena.api.server import create_app


def _data(response):
    assert response.status_code == 200, response.text
    return response.json()["data"]


def test_runtime_learning_requires_feedback_replay_shadow_and_review() -> None:
    repository = Path(__file__).parent / "fixtures" / "runtime_repo"
    with TestClient(create_app()) as client:
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
