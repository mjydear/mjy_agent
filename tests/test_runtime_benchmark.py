"""Acceptance tests for the reproducible Runtime benchmark."""

from __future__ import annotations

import json

from scripts.run_runtime_benchmark import _percentile, _render_markdown, run_benchmark


def test_percentile_uses_nearest_rank_and_handles_empty_input() -> None:
    assert _percentile([], 0.95) == 0.0
    assert _percentile([30.0, 10.0, 20.0], 0.50) == 20.0
    assert _percentile([30.0, 10.0, 20.0], 0.95) == 30.0


def test_one_run_benchmark_covers_runtime_governance_contracts() -> None:
    report = run_benchmark(1)

    assert report["measurement_scope"]["external_model_calls"] is False
    assert report["functional"]["success_rate"] == 1.0
    assert report["token_and_memory"]["goal_retention_rate_after_compaction"] == 1.0
    assert report["token_and_memory"]["evidence_retention_rate_after_compaction"] == 1.0
    assert report["token_and_memory"]["raw_artifact_prompt_inclusion_rate"] == 0.0
    assert report["tools"]["scope_case"]["path_out_of_scope_rejected"] is True
    assert report["effect_journal"]["passed"] is True
    assert report["effect_journal"]["duplicate_invocations"] == 0
    assert report["self_evolution"]["passed"] is True
    assert report["self_evolution"]["activation_allowed"] is False
    dataset = report["self_evolution"]["training_dataset"]
    assert dataset["training_ready"] is True
    assert dataset["example_count"] == 1
    assert dataset["quality_gate_probe_reason"] == "VERIFIED_OPERATOR_FEEDBACK_REQUIRED"
    assert dataset["no_raw_artifacts"] is True
    assert dataset["no_hidden_reasoning"] is True
    jsonl_records = [
        json.loads(line) for line in dataset["training_jsonl"].splitlines()
    ]
    assert len(jsonl_records) == dataset["example_count"]
    assert jsonl_records[0]["schema_version"] == "runtime.training.example.v1"


def test_markdown_report_declares_demo_measurement_boundary() -> None:
    report = run_benchmark(1)
    markdown = _render_markdown(report)

    assert "deterministic-demo" in markdown
    assert "不能外推为生产 QPS" in markdown
    assert "Artifact 原文进入模型上下文比例" in markdown
    assert "Effect Journal 崩溃恢复重复调用" in markdown
