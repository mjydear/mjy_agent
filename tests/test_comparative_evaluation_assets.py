"""Offline contract tests for the cross-system comparison assets."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASKS_PATH = ROOT / "benchmarks" / "agent-runtime" / "comparison-tasks.json"
PROTOCOL_PATH = ROOT / "docs" / "benchmarks" / "comparative_evaluation.md"


def _load_assets() -> tuple[dict, str]:
    tasks = json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    protocol = PROTOCOL_PATH.read_text(encoding="utf-8")
    return tasks, protocol


def test_comparison_task_asset_is_valid_and_has_unique_versioned_tasks() -> None:
    tasks, _ = _load_assets()

    assert tasks["schema_version"] == "agent-runtime.comparison-tasks.v1"
    assert tasks["measurement_policy"]["external_calls_in_repository"] is False
    assert tasks["measurement_policy"]["secrets_in_artifacts"] is False
    assert len(tasks["tasks"]) >= 5

    task_ids: set[str] = set()
    for task in tasks["tasks"]:
        task_ids.add(task["task_id"])
        assert task["task_version"]
        assert task["user_input"]
        assert isinstance(task["fixture"], dict)
        assert task["tool_contract"]["side_effects"] in {
            "none",
            "read_only",
            "destructive",
        }
        assert task["timeout_seconds"] > 0
        assert task["success_criteria"]["all"]
        assert "required_approval" in task["safety_policy"]

    assert len(task_ids) == len(tasks["tasks"])


def test_task_set_covers_quality_tools_memory_workflow_and_safety() -> None:
    tasks, _ = _load_assets()
    categories = {task["category"] for task in tasks["tasks"]}

    assert {
        "structured_output",
        "tool_recovery",
        "long_context",
        "react_workflow",
        "memory",
        "safety",
    } <= categories


def test_protocol_forbids_fake_external_api_equivalence_and_fake_metrics() -> None:
    _, protocol = _load_assets()

    required_phrases = (
        "不能被本仓库直接假装成同一个 API",
        "独立 Adapter",
        "not_comparable",
        "usage.source",
        "cost.amount",
        "human_operation",
        "task_package_sha256",
        "本仓库没有执行 Claude Code、OpenClaw 或其他外部系统的真实实验",
    )
    for phrase in required_phrases:
        assert phrase in protocol


def test_protocol_contains_executable_task_and_result_schema_examples() -> None:
    _, protocol = _load_assets()

    assert '"task_id": "tool-retry-002"' in protocol
    assert '"schema_version": "agent-runtime.comparison-result.v1"' in protocol
    assert '"input_tokens": 1234' in protocol
    assert '"source": "provider_reported"' in protocol
    assert '"overall": "partially_comparable"' in protocol
    assert "same_task_package" in protocol
    assert "required_step_order" in protocol
