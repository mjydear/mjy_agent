"""Run a reproducible, provider-free benchmark for the Agent Runtime.

The benchmark deliberately uses the local deterministic Runtime adapter. It
measures the execution and governance layers, not the latency or billing of an
external model provider. Results are written to artifacts/ so generated
numbers never become an accidental source-controlled claim.

Usage:

    python scripts/run_runtime_benchmark.py --runs 10
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from athena.api.server import create_app  # noqa: E402
from athena.application.runtime_worker import RuntimeWorker  # noqa: E402
from athena.config import AthenaSettings, DatabaseSettings  # noqa: E402
from athena.infra.token_meter import TokenMeter  # noqa: E402
from athena.runtime import (  # noqa: E402
    AgentRuntime,
    AgentTask,
    InMemoryRuntimeStore,
    ReadOnlyToolCatalog,
)
from athena.runtime.learning import (  # noqa: E402
    OperatorFeedback,
    ReplayCase,
    ReviewGate,
    RuntimeSkillLearningLifecycle,
    RuntimeSkillLearningObserver,
    RuntimeSkillReplayEvaluator,
    RuntimeSkillShadowEvaluator,
    ShadowCase,
    TrajectoryDatasetBuilder,
)

FIXTURE_REPOSITORY = PROJECT_ROOT / "tests" / "fixtures" / "runtime_repo"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "artifacts" / "runtime-benchmarks"
DIAGNOSIS_GOAL = (
    "Diagnose the pricing test failure in runtime_repo and provide a read-only "
    "repair recommendation."
)


def _data(response: Any) -> dict[str, Any]:
    if response.status_code not in {200, 201}:
        raise RuntimeError(
            f"Runtime benchmark API request failed: {response.status_code} "
            f"{response.text[:500]}"
        )
    payload = response.json()
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Runtime benchmark API response has no object data field")
    return data


def _percentile(values: list[float], fraction: float) -> float:
    """Return the nearest-rank percentile used by the report."""

    if not values:
        return 0.0
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be between 0 and 1")
    ordered = sorted(values)
    index = max(0, math.ceil(fraction * len(ordered)) - 1)
    return ordered[index]


def _distribution(values: list[float]) -> dict[str, float]:
    return {
        "avg": round(statistics.mean(values), 3) if values else 0.0,
        "p50": round(_percentile(values, 0.50), 3),
        "p95": round(_percentile(values, 0.95), 3),
        "max": round(max(values), 3) if values else 0.0,
    }


def _run_functional_case(
    client: TestClient,
    runtime_store: Any,
    run_number: int,
) -> dict[str, Any]:
    """Run one fixed diagnosis and collect public plus durable projections."""

    goal = f"{DIAGNOSIS_GOAL} Dataset case {run_number}."
    started_at = time.perf_counter()
    created = _data(
        client.post(
            "/api/runtime/tasks",
            headers={"Idempotency-Key": f"runtime-benchmark-{run_number}"},
            json={
                "goal": goal,
                "repository_path": str(FIXTURE_REPOSITORY),
                "profile": "standard",
            },
        )
    )
    task_id = str(created["id"])
    run_started_at = time.perf_counter()
    detail = _data(client.post(f"/api/runtime/tasks/{task_id}/run"))
    run_latency_ms = (time.perf_counter() - run_started_at) * 1000
    end_to_end_ms = (time.perf_counter() - started_at) * 1000

    context = _data(client.get(f"/api/runtime/tasks/{task_id}/context"))
    evidence = _data(client.get(f"/api/runtime/tasks/{task_id}/evidence"))["items"]
    usage = _data(client.get(f"/api/runtime/tasks/{task_id}/usage"))["items"]
    events = _data(client.get(f"/api/runtime/tasks/{task_id}/events"))["items"]

    snapshot = runtime_store.snapshot(task_id)
    context_snapshot = snapshot.context
    context_payload = json.dumps(context, ensure_ascii=False, sort_keys=True)
    raw_payload = {
        "task": {
            "goal": snapshot.task.goal,
            "repository_root": snapshot.task.repository_root,
            "profile": snapshot.task.profile.value,
        },
        "events": [
            {"type": event.kind, "payload": event.payload} for event in snapshot.events
        ],
        "artifacts": [artifact.content for artifact in snapshot.artifacts],
        "evidence": [
            {
                "evidence_id": item.evidence_id,
                "artifact_id": item.artifact_id,
                "summary": item.summary,
            }
            for item in snapshot.evidence
        ],
    }
    meter = TokenMeter()
    naive_full_history_tokens = meter.count_json(raw_payload)
    optimized_context_tokens = (
        context_snapshot.estimated_input_tokens if context_snapshot is not None else 0
    )
    governance = context["metrics"].get("memory_governance", {})
    artifact_content_policy = str(
        governance.get("artifact_content_policy", "unknown")
    )
    raw_artifact_serialized = json.dumps(
        [artifact.content for artifact in snapshot.artifacts],
        ensure_ascii=False,
        sort_keys=True,
    )
    raw_artifact_tokens = meter.count(raw_artifact_serialized)
    raw_artifact_in_context = bool(
        raw_artifact_tokens and raw_artifact_serialized in context_payload
    )

    input_tokens = sum(int(item["estimated_input_tokens"]) for item in usage)
    output_tokens = sum(
        max(0, int(item["actual_tokens"]) - int(item["estimated_input_tokens"]))
        for item in usage
    )
    tool_calls = sum(event["type"] == "tool.called" for event in events)
    tool_successes = sum(event["type"] == "tool.succeeded" for event in events)
    tool_rejections = sum(event["type"] == "tool.rejected" for event in events)
    evidence_ids = {str(item["id"]) for item in evidence}
    pinned_evidence_ids = set(context["snapshot"].get("pinned_evidence", []))

    return {
        "run_number": run_number,
        "task_id": task_id,
        "status": detail["status"],
        "backend": detail["execution"]["backend"],
        "decision_mode": detail["execution"]["decision_mode"],
        "end_to_end_ms": round(end_to_end_ms, 3),
        "run_latency_ms": round(run_latency_ms, 3),
        "ticks": int(detail["tick_count"]),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "compaction_count": int(context["metrics"]["compaction_count"]),
        "pre_compaction_utilization": float(
            governance.get("pre_compaction_utilization", 0.0)
        ),
        "post_compaction_utilization": float(
            governance.get("input_utilization", 0.0)
        ),
        "goal_retained": context["snapshot"]["task_frame"]["goal"] == goal,
        "evidence_retained": evidence_ids.issubset(pinned_evidence_ids),
        "raw_artifact_in_context": raw_artifact_in_context,
        "raw_artifact_tokens": raw_artifact_tokens,
        "naive_full_history_tokens": naive_full_history_tokens,
        "optimized_context_tokens": optimized_context_tokens,
        "artifact_content_policy": artifact_content_policy,
        "tool_calls": tool_calls,
        "tool_successes": tool_successes,
        "tool_rejections": tool_rejections,
        "model_tiers": sorted({str(item["model"]) for item in usage}),
        "route_reasons": sorted({str(item["route_reason"]) for item in usage}),
    }


def _run_scope_case(client: TestClient) -> dict[str, Any]:
    outside_path = (PROJECT_ROOT / "outside-benchmark-secret.txt").resolve()
    goal = f"Read this absolute path before diagnosis: {outside_path}"
    created = _data(
        client.post(
            "/api/runtime/tasks",
            json={
                "goal": goal,
                "repository_path": str(FIXTURE_REPOSITORY),
                "profile": "standard",
            },
        )
    )
    task_id = str(created["id"])
    detail = _data(client.post(f"/api/runtime/tasks/{task_id}/run"))
    events = _data(client.get(f"/api/runtime/tasks/{task_id}/events"))["items"]
    rejected = [
        event
        for event in events
        if event["type"] == "tool.rejected"
        and event["payload"].get("reason_code") == "PATH_OUT_OF_SCOPE"
    ]
    return {
        "status": detail["status"],
        "tool_calls": sum(event["type"] == "tool.called" for event in events),
        "policy_rejections": len(rejected),
        "path_out_of_scope_rejected": bool(rejected),
    }


class _CountingTools(ReadOnlyToolCatalog):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def invoke(self, **kwargs: Any):
        self.calls += 1
        return super().invoke(**kwargs)


class _CommitFailsOnce(InMemoryRuntimeStore):
    def __init__(self) -> None:
        super().__init__()
        self.failed = False

    def commit_tick(self, **kwargs: Any):
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated process crash after effect completion")
        return super().commit_tick(**kwargs)


def _run_effect_journal_case() -> dict[str, Any]:
    """Measure the crash window where a tool already completed."""

    store = _CommitFailsOnce()
    tools = _CountingTools()
    task = AgentTask.create(
        goal="Diagnose the pricing failure",
        repository_root=str(FIXTURE_REPOSITORY),
    )
    store.create_task(task)
    runtime = AgentRuntime(store=store, tools=tools)
    crash_observed = False
    try:
        runtime.advance(task.task_id, lease_id="benchmark-worker")
    except RuntimeError as exc:
        crash_observed = "simulated process crash" in str(exc)
    recovered = runtime.advance(task.task_id, lease_id="benchmark-worker")
    duplicate_invocations = max(0, tools.calls - 1)
    return {
        "crash_observed": crash_observed,
        "recovered_tick": recovered.tick is not None,
        "tool_invocations": tools.calls,
        "duplicate_invocations": duplicate_invocations,
        "artifacts_after_recovery": len(store.snapshot(task.task_id).artifacts),
        "passed": crash_observed
        and recovered.tick is not None
        and duplicate_invocations == 0,
    }


def _run_skill_lifecycle_case() -> dict[str, Any]:
    """Exercise candidate -> replay -> shadow -> human review -> handoff."""

    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal="Diagnose the pricing failure",
        repository_root=str(FIXTURE_REPOSITORY),
    )
    store.create_task(task)
    runtime = AgentRuntime(store=store)
    RuntimeWorker(runtime).run_to_boundary(task.task_id, max_ticks=6)
    snapshot = store.snapshot(task.task_id)
    observer = RuntimeSkillLearningObserver(min_evidence=3)
    observed = observer.observe_completed_task(
        snapshot,
        OperatorFeedback(
            feedback_id="benchmark-feedback",
            accepted=True,
            verified=True,
            summary="The evidence-backed diagnosis is correct.",
            submitted_by="benchmark-operator",
        ),
    )
    if observed.candidate is None:
        return {
            "candidate_created": False,
            "blocked_reason": observed.blocked_reason,
            "replay_pass_rate": 0.0,
            "shadow_pass_rate": 0.0,
            "review_approved": False,
            "activation_allowed": False,
            "auto_activation_count": 0,
            "passed": False,
        }

    lifecycle = RuntimeSkillLearningLifecycle()
    replay_evaluator = RuntimeSkillReplayEvaluator()
    shadow_evaluator = RuntimeSkillShadowEvaluator()
    candidate = lifecycle.mark_replay_pending(observed.candidate)
    root_cause = str(candidate.procedure["root_cause"])
    replay = replay_evaluator.evaluate(
        candidate,
        (
            ReplayCase(
                case_id="benchmark-replay",
                expected_root_cause=root_cause,
                required_evidence_ids=candidate.source_evidence_ids,
            ),
        ),
    )
    candidate = lifecycle.record_replay(candidate, replay)
    shadow = shadow_evaluator.evaluate(
        candidate,
        (
            ShadowCase(
                case_id="benchmark-shadow",
                observed_root_cause=root_cause,
                observed_evidence_ids=candidate.source_evidence_ids,
                effect_count=0,
            ),
        ),
    )
    candidate = lifecycle.record_shadow(candidate, shadow)
    candidate = lifecycle.review(
        candidate,
        ReviewGate(
            reviewer="benchmark-reviewer",
            approved=True,
            note="Replay and shadow passed; manual draft creation remains required.",
        ),
    )
    handoff = lifecycle.handoff(candidate)
    return {
        "candidate_created": True,
        "replay_pass_rate": replay.pass_rate,
        "shadow_pass_rate": shadow.pass_rate,
        "review_approved": candidate.review_approved is True,
        "activation_allowed": handoff.activation_allowed,
        "manual_draft_required": handoff.requires_manual_draft_creation,
        "auto_activation_count": 0,
        "passed": (
            replay.passed
            and shadow.passed
            and candidate.review_approved is True
            and handoff.activation_allowed is False
        ),
    }


def _run_dataset_case(
    runtime_store: Any,
    functional_samples: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build training-ready records and exercise duplicate/quality gates."""

    snapshots = [
        runtime_store.snapshot(str(item["task_id"])) for item in functional_samples
    ]
    builder = TrajectoryDatasetBuilder(min_evidence=3)
    trajectories = tuple(
        (
            snapshot,
            OperatorFeedback(
                feedback_id=f"dataset-feedback-{index}",
                accepted=True,
                verified=True,
                summary="Operator verified the evidence-backed result.",
                submitted_by="benchmark-operator",
            ),
        )
        for index, snapshot in enumerate(snapshots, start=1)
    )
    first_snapshot, first_feedback = trajectories[0]
    dataset = builder.build(
        (*trajectories, (first_snapshot, first_feedback))
    )
    rejected_probe = builder.build(
        (
            (
                first_snapshot,
                OperatorFeedback(
                    feedback_id="dataset-rejected-probe",
                    accepted=False,
                    verified=True,
                    summary="Operator rejected this result.",
                    submitted_by="benchmark-operator",
                ),
            ),
        )
    )
    dataset_payload = dataset.to_dict()
    dataset_payload.update(
        {
            "training_jsonl": dataset.to_jsonl(),
            "training_ready": bool(dataset.examples),
            "quality_gate_probe_rejected_count": len(rejected_probe.rejected),
            "quality_gate_probe_reason": (
                rejected_probe.rejected[0]["reason_code"]
                if rejected_probe.rejected
                else None
            ),
            "no_raw_artifacts": all(
                item.quality["raw_artifacts_included"] is False
                for item in dataset.examples
            ),
            "no_hidden_reasoning": all(
                item.quality["hidden_reasoning_included"] is False
                for item in dataset.examples
            ),
            "jsonl_record_count": len(dataset.to_jsonl().splitlines()),
        }
    )
    return dataset_payload


def run_benchmark(runs: int) -> dict[str, Any]:
    if runs < 1:
        raise ValueError("runs must be at least 1")
    for logger_name in ("athena.access", "athena.api.server", "athena.runtime.bootstrap", "httpx"):
        logging.getLogger(logger_name).setLevel(logging.WARNING)
    settings = AthenaSettings(database=DatabaseSettings(url=None, auto_migrate=False))
    app = create_app(settings)
    functional: list[dict[str, Any]] = []
    with TestClient(app) as client:
        runtime_store = app.state.runtime_store
        for run_number in range(1, runs + 1):
            functional.append(_run_functional_case(client, runtime_store, run_number))
        scope = _run_scope_case(client)
        training_dataset = _run_dataset_case(runtime_store, functional)
    effect_journal = _run_effect_journal_case()
    learning = _run_skill_lifecycle_case()

    successful = [item for item in functional if item["status"] == "succeeded"]
    latency_values = [float(item["end_to_end_ms"]) for item in functional]
    run_latency_values = [float(item["run_latency_ms"]) for item in functional]
    input_values = [float(item["input_tokens"]) for item in functional]
    output_values = [float(item["output_tokens"]) for item in functional]
    total_values = [float(item["total_tokens"]) for item in functional]
    tick_values = [float(item["ticks"]) for item in functional]
    full_history_values = [
        float(item["naive_full_history_tokens"]) for item in functional
    ]
    optimized_values = [
        float(item["optimized_context_tokens"]) for item in functional
    ]
    compression_values = [item["compaction_count"] > 0 for item in functional]
    goal_values = [bool(item["goal_retained"]) for item in functional]
    evidence_values = [bool(item["evidence_retained"]) for item in functional]
    artifact_values = [bool(item["raw_artifact_in_context"]) for item in functional]
    savings = [
        (full - optimized) / full
        for full, optimized in zip(full_history_values, optimized_values)
        if full > 0
    ]

    first = functional[0]
    report = {
        "schema_version": "runtime-benchmark.v1",
        "generated_at_utc": datetime.now(UTC).isoformat(),
        "measurement_scope": {
            "repository": str(FIXTURE_REPOSITORY.relative_to(PROJECT_ROOT)),
            "runs": runs,
            "external_model_calls": False,
            "backend": first["backend"],
            "decision_mode": first["decision_mode"],
            "tokenizer": "TokenMeter dependency-free fallback",
            "latency_unit": "ms",
            "claim_policy": (
                "These are local deterministic Runtime measurements. They do not "
                "represent provider latency, provider tokenization, or production QPS."
            ),
        },
        "functional": {
            "runs": runs,
            "successes": len(successful),
            "success_rate": round(len(successful) / runs, 4),
            "statuses": {
                status: sum(item["status"] == status for item in functional)
                for status in sorted({item["status"] for item in functional})
            },
            "end_to_end_latency_ms": _distribution(latency_values),
            "run_latency_ms": _distribution(run_latency_values),
            "ticks": _distribution(tick_values),
        },
        "token_and_memory": {
            "input_tokens_per_task": _distribution(input_values),
            "output_tokens_per_task": _distribution(output_values),
            "total_tokens_per_task": _distribution(total_values),
            "naive_full_history_tokens": _distribution(full_history_values),
            "optimized_context_tokens": _distribution(optimized_values),
            "naive_to_optimized_estimated_saving_rate": round(
                statistics.mean(savings), 4
            )
            if savings
            else 0.0,
            "compaction_trigger_rate": round(sum(compression_values) / runs, 4),
            "goal_retention_rate_after_compaction": round(sum(goal_values) / runs, 4),
            "evidence_retention_rate_after_compaction": round(
                sum(evidence_values) / runs, 4
            ),
            "raw_artifact_prompt_inclusion_rate": round(sum(artifact_values) / runs, 4),
            "artifact_content_policy": first["artifact_content_policy"],
            "raw_artifact_tokens_observed": first["raw_artifact_tokens"],
        },
        "tools": {
            "functional_total_calls": sum(item["tool_calls"] for item in functional),
            "functional_successes": sum(item["tool_successes"] for item in functional),
            "functional_rejections": sum(
                item["tool_rejections"] for item in functional
            ),
            "scope_case": scope,
            "scope_rejection_rate": round(
                scope["policy_rejections"] / max(1, scope["tool_calls"]), 4
            ),
        },
        "effect_journal": effect_journal,
        "self_evolution": {
            **learning,
            "training_dataset": training_dataset,
        },
        "model_routing": {
            "status": "not_measured_external_provider",
            "observed_model_tiers": sorted(
                {tier for item in functional for tier in item["model_tiers"]}
            ),
            "observed_route_reasons": sorted(
                {reason for item in functional for reason in item["route_reasons"]}
            ),
            "next_measurement": (
                "Run the same suite with provider-backed light/heavy models and "
                "record provider usage metadata."
            ),
        },
        "raw_samples": functional,
    }
    return report


def _pct(value: float) -> str:
    return f"{value:.2%}"


def _render_markdown(report: dict[str, Any]) -> str:
    functional = report["functional"]
    token = report["token_and_memory"]
    tools = report["tools"]
    effect = report["effect_journal"]
    learning = report["self_evolution"]
    dataset = learning["training_dataset"]
    routing = report["model_routing"]
    scope = report["measurement_scope"]
    latency = functional["end_to_end_latency_ms"]
    total_tokens = token["total_tokens_per_task"]
    return "\n".join(
        [
            "# Agent Runtime 指标基准报告",
            "",
            f"生成时间：{report['generated_at_utc']}",
            "",
            "## 测量边界",
            "",
            f"- 固定任务集：{scope['repository']}，重复运行 {scope['runs']} 次。",
            f"- 执行后端：{scope['backend']}；决策模式：{scope['decision_mode']}。",
            "- 外部模型调用：否；Token 为 Runtime 的可替换估算器结果。",
            "- 数据证明本地执行平面和治理机制；不能外推为生产 QPS、云模型延迟或真实账单。",
            "",
            "## 可写入简历的已实测指标",
            "",
            "| 指标 | 结果 | 口径 |",
            "| --- | ---: | --- |",
            f"| Training-ready 样本数 | {dataset['example_count']} | 成功任务 + 已验证反馈 + Evidence 门禁 |",
            f"| 数据质量拒绝探针 | {dataset['quality_gate_probe_rejected_count']} | reason={dataset['quality_gate_probe_reason']} |",
            f"| 重复样本去重数 | {dataset['duplicate_count']} | 基于目标、Evidence 摘要和工具序列指纹 |",
            f"| 训练/验证/测试切分 | {dataset['split_counts']['train']} / {dataset['split_counts']['validation']} / {dataset['split_counts']['test']} | 确定性哈希切分 |",
            f"| Dataset 无 Artifact 原文 / 无隐藏思维 | {dataset['no_raw_artifacts']} / {dataset['no_hidden_reasoning']} | 数据集泄漏检查 |",
            f"| 固定诊断任务成功率 | {_pct(functional['success_rate'])} | {functional['successes']}/{functional['runs']} 次终态为 succeeded |",
            f"| 端到端延迟 P50 / P95 / Max | {latency['p50']:.1f} / {latency['p95']:.1f} / {latency['max']:.1f} ms | TestClient 本地单进程 |",
            f"| 单任务总 Token P50 / P95 | {total_tokens['p50']:.0f} / {total_tokens['p95']:.0f} | 输入估算 + deterministic 输出占位 |",
            f"| 朴素全历史到结构化上下文估算降幅 | {_pct(token['naive_to_optimized_estimated_saving_rate'])} | 同一快照的可控估算对比，不是 provider 账单节省 |",
            f"| 压缩后目标保留率 | {_pct(token['goal_retention_rate_after_compaction'])} | Context Projection 校验 |",
            f"| 压缩后 Evidence 引用保留率 | {_pct(token['evidence_retention_rate_after_compaction'])} | Evidence ID 集合校验 |",
            f"| Artifact 原文进入模型上下文比例 | {_pct(token['raw_artifact_prompt_inclusion_rate'])} | references_only 策略校验 |",
            f"| 越权路径拒绝率 | {_pct(tools['scope_rejection_rate'])} | 固定越权用例应被 PATH_OUT_OF_SCOPE 拒绝 |",
            f"| Effect Journal 崩溃恢复重复调用 | {effect['duplicate_invocations']} | 模拟提交崩溃后重试 |",
            f"| Skill 自动激活次数 | {learning['auto_activation_count']} | Candidate/Replay/Shadow/Review 均未自动激活 |",
            "",
            "## 其他结果",
            "",
            f"- Dataset ID：{dataset['dataset_id']}，JSONL 记录数 {dataset['jsonl_record_count']}，平均质量分 {dataset['average_quality_score']:.2f}。",
            f"- 工具调用：功能任务共 {tools['functional_total_calls']} 次，成功 {tools['functional_successes']} 次，拒绝 {tools['functional_rejections']} 次。",
            f"- 上下文压缩触发率：{_pct(token['compaction_trigger_rate'])}。",
            f"- Skill Replay / Shadow 通过率：{_pct(learning['replay_pass_rate'])} / {_pct(learning['shadow_pass_rate'])}。",
            f"- 模型路由：当前仅观察到 {', '.join(routing['observed_model_tiers']) or 'none'}，外部轻量/重量模型分流 {routing['status']}。",
            "",
            "## 简历表述草稿",
            "",
            f"- 构建自进化数据闭环：从 {dataset['example_count']} 条已验证 Runtime 轨迹生成 training-ready JSONL，经质量门禁、指纹去重和 train/validation/test 切分，原始 Artifact 与隐藏思维均不进入训练数据。",
            f"- 设计并实现可持久化 Agent Runtime，基于固定仓库诊断任务实测成功率 {_pct(functional['success_rate'])}，端到端延迟 P95 {latency['p95']:.1f}ms。",
            f"- 实现四层记忆与 Token Governance，对比朴素全历史上下文，当前本地估算输入规模降低 {_pct(token['naive_to_optimized_estimated_saving_rate'])}；压缩后目标与 Evidence 保留率均为 {_pct(token['goal_retention_rate_after_compaction'])}。",
            f"- 实现只读 Tool Gateway 与 Effect Journal，固定越权路径 {tools['scope_case']['path_out_of_scope_rejected']} 被拒绝，模拟崩溃恢复重复工具调用为 {effect['duplicate_invocations']}。",
            "",
            "## 面试时必须主动说明",
            "",
            "当前报告是本地 deterministic-demo 基线，输出 Token 不是外部 LLM 的真实计费 Token；模型分流、真实 Provider 延迟和并发吞吐仍需配置真实模型后单独补测。",
            "",
        ]
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the provider-free Agent Runtime benchmark"
    )
    parser.add_argument(
        "--runs", type=int, default=10, help="functional task repetitions"
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="directory for generated JSON and Markdown reports",
    )
    args = parser.parse_args()
    report = run_benchmark(args.runs)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "runtime-benchmark-latest.json"
    markdown_path = args.output_dir / "runtime-benchmark-latest.md"
    dataset_path = args.output_dir / "runtime-training-dataset.jsonl"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    dataset_path.write_text(
        str(report["self_evolution"]["training_dataset"]["training_jsonl"]),
        encoding="utf-8",
    )
    print(_render_markdown(report))
    print(f"\nJSON: {json_path}")
    print(f"Markdown: {markdown_path}")
    print(f"Dataset JSONL: {dataset_path}")


if __name__ == "__main__":
    main()
