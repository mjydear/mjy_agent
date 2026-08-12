"""Runtime execution seam and deterministic code-diagnosis acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from athena.api.server import create_app


FIXTURE_REPOSITORY = Path(__file__).parent / "fixtures" / "runtime_repo"


def _data(response) -> dict[str, Any]:
    assert response.status_code == 200, response.text
    payload = response.json()
    assert isinstance(payload.get("data"), dict)
    return payload["data"]


def _create_diagnosis_task(client: TestClient) -> tuple[str, str]:
    goal = (
        "诊断 runtime_repo 中的价格计算测试失败：原价 10000 分，"
        "折扣 20% 后应该得到 8000 分。找出根因并给出不执行写入的修复建议。"
    )
    task = _data(
        client.post(
            "/api/runtime/tasks",
            headers={"Idempotency-Key": "runtime-pricing-diagnosis"},
            json={
                "goal": goal,
                "repository_path": str(FIXTURE_REPOSITORY),
                "profile": "standard",
            },
        )
    )
    return task["id"], goal


def _events(client: TestClient, task_id: str) -> list[dict[str, Any]]:
    events = _data(client.get(f"/api/runtime/tasks/{task_id}/events"))["items"]
    assert [event["sequence"] for event in events] == sorted(
        event["sequence"] for event in events
    )
    return events


def _assert_no_hidden_reasoning(payload: Any) -> None:
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    assert "chain_of_thought" not in serialized
    assert "raw_thought" not in serialized
    assert "hidden_reasoning" not in serialized


def test_runtime_runs_local_repository_to_sourced_report_and_inspectable_projections() -> None:
    with TestClient(create_app()) as client:
        task_id, goal = _create_diagnosis_task(client)
        run_result = _data(client.post(f"/api/runtime/tasks/{task_id}/run"))
        assert run_result["id"] == task_id

        detail = _data(client.get(f"/api/runtime/tasks/{task_id}"))
        assert detail["status"] == "succeeded"
        assert detail["report"]["root_cause"]
        assert detail["report"]["repair_recommendation"]
        assert detail["report"]["evidence_ids"]

        events = _events(client, task_id)
        tick_events = [event for event in events if event["type"] == "tick.completed"]
        assert tick_events
        assert len(tick_events) <= 6
        _assert_no_hidden_reasoning(events)

        evidence = _data(client.get(f"/api/runtime/tasks/{task_id}/evidence"))["items"]
        assert evidence
        assert all(card["id"] and card["source"] for card in evidence)
        assert any(card.get("artifact_id") for card in evidence)
        assert set(detail["report"]["evidence_ids"]).issubset(
            {card["id"] for card in evidence}
        )

        context = _data(client.get(f"/api/runtime/tasks/{task_id}/context"))
        assert context["snapshot"]["task_frame"]["goal"] == goal
        assert context["snapshot"]["pinned_evidence"]
        _assert_no_hidden_reasoning(context)

        usage = _data(client.get(f"/api/runtime/tasks/{task_id}/usage"))["items"]
        assert usage
        assert all(
            entry["purpose"]
            and entry["model"]
            and entry["route_reason"]
            and entry["reserved_tokens"] >= entry["actual_tokens"]
            for entry in usage
        )


def test_long_tool_artifact_compacts_context_without_losing_goal_or_evidence() -> None:
    with TestClient(create_app()) as client:
        task_id, goal = _create_diagnosis_task(client)
        _data(client.post(f"/api/runtime/tasks/{task_id}/run"))

        context = _data(client.get(f"/api/runtime/tasks/{task_id}/context"))
        snapshot = context["snapshot"]
        assert snapshot["task_frame"]["goal"] == goal
        assert context["metrics"]["compaction_count"] >= 1
        assert snapshot["running_summary"]

        evidence = _data(client.get(f"/api/runtime/tasks/{task_id}/evidence"))["items"]
        evidence_ids = {card["id"] for card in evidence}
        assert evidence_ids
        assert evidence_ids.intersection(snapshot["pinned_evidence"])
        _assert_no_hidden_reasoning(context)
