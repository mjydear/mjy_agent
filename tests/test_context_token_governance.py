"""Focused tests for token metering and context budget governance."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from athena.agent.context import (
    ContextBudgetError,
    ContextCompiler,
    ContextManager,
)
from athena.agent.policy.contracts import (
    DataOrigin,
    EnvironmentMode,
    ExecutionProfile,
    RiskLevel,
    ToolSpecV2,
)
from athena.agent.workflow.state import OpsTaskState, TaskBudget
from athena.infra.token_meter import TokenMeter
from athena.memory import WorkingMemory
from athena.memory.evidence import Evidence


def _state(*, remaining_tokens: int = 2_000) -> OpsTaskState:
    return OpsTaskState(
        task_id="task-token-1",
        tenant_id="tenant-a",
        objective="diagnose payment pod",
        environment_id="prod-a",
        environment_mode=EnvironmentMode.MOCK,
        scope={"namespace": "payment", "pod": "api-0"},
        tenant_policy_snapshot={"readonly": True},
        budget=TaskBudget(
            remaining_steps=4,
            remaining_tokens=remaining_tokens,
            remaining_time_ms=30_000,
        ),
        execution_profile=ExecutionProfile.BOUNDED_POLICY_LOOP,
        facts=({"stable_fact": "keep", "thought": "hidden reasoning"},),
    )


def _evidence(identifier: str, summary: str) -> Evidence:
    observed_at = datetime(2026, 8, 9, 10, 0, tzinfo=UTC)
    return Evidence(
        id=identifier,
        tenant_id="tenant-a",
        task_id="task-token-1",
        type="log",
        source="k8s.logs.read",
        data_origin=DataOrigin.MOCK,
        summary=summary,
        content_ref=None,
        content_hash=f"hash-{identifier}",
        observed_at=observed_at,
        collected_at=observed_at,
    )


def _tool(name: str) -> ToolSpecV2:
    return ToolSpecV2(
        name=name,
        version="1.0.0",
        domain="kubernetes",
        input_schema={
            "type": "object",
            "properties": {"namespace": {"type": "string"}},
        },
        output_schema={"type": "object"},
        required_capabilities=("k8s.read",),
        risk_level=RiskLevel.S1,
        readonly=True,
        idempotent=True,
        timeout_seconds=10,
    )


def test_fallback_meter_counts_each_cjk_character_conservatively() -> None:
    meter = TokenMeter()

    assert meter.count("你好世界") >= 4
    assert meter.count("故障诊断：容器反复重启") >= len("故障诊断：容器反复重启")


def test_model_tokenizer_is_an_injectable_meter_adapter() -> None:
    calls: list[str] = []

    class FakeTokenizer:
        def encode(self, text: str, *, add_special_tokens: bool = False) -> list[int]:
            calls.append(text)
            assert add_special_tokens is False
            return list(range(7))

    meter = TokenMeter(tokenizer=FakeTokenizer())

    assert meter.count("任意文本") == 7
    assert calls == ["任意文本"]


def test_context_compiler_preserves_constraints_and_packs_by_priority() -> None:
    # A character-count adapter makes the test budget deterministic while still
    # exercising the same public meter seam used by a model tokenizer.
    meter = TokenMeter(tokenizer=lambda text: len(text))
    manager = ContextManager(token_meter=meter)
    context = manager.build(
        _state(),
        (
            _evidence("evidence-1", "OOMKilled root signal"),
            _evidence("evidence-2", "older supporting signal"),
        ),
        (_tool("k8s.pod.get"),),
        knowledge_references=("incident reference " * 30,),
        skill_references=("skill reference " * 30,),
        input_budget_tokens=620,
        output_reserve_tokens=80,
        task_budget_tokens=2_000,
    )

    assert context.payload["task"]["scope"] == {
        "namespace": "payment",
        "pod": "api-0",
    }
    assert context.payload["verified_facts"] == [{"stable_fact": "keep"}]
    assert len(context.payload["evidence"]) >= 1
    assert context.payload["knowledge_references"] == []
    assert context.payload["skill_references"] == []
    assert context.payload["tool_schemas"] == []
    assert context.compression_metrics["budget_truncated_evidence"] == 1
    assert context.compression_metrics["budget_minimum_evidence_overflow"] == 1
    assert context.compression_metrics["budget_truncated_knowledge"] == 1
    assert context.compression_metrics["budget_truncated_skill"] == 1
    assert context.compression_metrics["budget_truncated_tools"] == 1
    assert context.compression_metrics["output_reserve_tokens"] == 80
    assert context.compression_metrics["available_input_tokens"] == 620


def test_output_reserve_can_reject_or_degrade_without_dropping_scope() -> None:
    manager = ContextCompiler(
        token_meter=TokenMeter(tokenizer=lambda text: len(text)),
        overflow_policy="reject",
    )

    with pytest.raises(ContextBudgetError) as exc_info:
        manager.compile(
            _state(remaining_tokens=10),
            (),
            (),
            output_reserve_tokens=20,
        )
    assert exc_info.value.reason_code == "OUTPUT_RESERVE_EXCEEDS_TASK_BUDGET"

    degraded = ContextManager(
        token_meter=TokenMeter(tokenizer=lambda text: len(text)),
        overflow_policy="degrade",
    ).build(
        _state(remaining_tokens=10),
        (),
        (),
        output_reserve_tokens=20,
    )
    assert degraded.payload["task"]["scope"] == {
        "namespace": "payment",
        "pod": "api-0",
    }
    assert degraded.compression_metrics["budget_degraded"] == 1
    assert degraded.compression_metrics["budget_output_reserve_exhausted"] == 1


def test_context_budget_metrics_expose_single_and_task_limits() -> None:
    context = ContextManager().build(
        _state(remaining_tokens=900),
        (),
        (),
        input_budget_tokens=700,
        output_reserve_tokens=100,
        task_budget_tokens=900,
    )

    metrics = context.compression_metrics
    assert metrics["input_budget_tokens"] == 700
    assert metrics["task_budget_tokens"] == 900
    assert metrics["output_reserve_tokens"] == 100
    assert metrics["available_input_tokens"] == 700
    assert metrics["budget_truncated_evidence"] == 0


def test_context_manager_without_explicit_budget_keeps_existing_context_shape() -> None:
    context = ContextManager().build(
        _state(remaining_tokens=2_000),
        (_evidence("evidence-1", "safe signal"),),
        (_tool("k8s.pod.get"),),
    )

    assert context.available_actions == ("k8s.pod.get",)
    assert context.payload["evidence"][0]["summary"] == "safe signal"
    assert context.payload["tool_schemas"][0]["name"] == "k8s.pod.get"
    assert context.compression_metrics["tokens_after"] == context.estimated_tokens
    assert "thought" not in str(context.payload).lower()
    assert "session_memory" not in context.payload


def test_working_memory_clips_a_single_oversized_message_at_read_time() -> None:
    meter = TokenMeter(tokenizer=lambda text: len(text))
    memory = WorkingMemory(max_tokens=4, token_meter=meter)
    memory.add_message("user", "x" * 80, importance=2.0)

    rendered = memory.render_for_budget(12)

    assert meter.count(memory.messages[0].content) <= 4
    assert meter.count(rendered) <= 12
    assert rendered.startswith("user: ")


def test_context_manager_packs_session_memory_under_the_shared_budget() -> None:
    meter = TokenMeter(tokenizer=lambda text: len(text))
    manager = ContextManager(token_meter=meter)
    memory = WorkingMemory(max_tokens=2_000, token_meter=meter)
    memory.add_message("tool", "discarded history " * 32, importance=0.1)
    memory.add_message("user", "keep-current", importance=2.0)

    baseline = manager.build(
        _state(),
        (),
        (),
        input_budget_tokens=10_000,
        task_budget_tokens=10_000,
    )
    context = manager.build(
        _state(),
        (),
        (),
        session_memory=memory,
        input_budget_tokens=baseline.estimated_tokens + 130,
        task_budget_tokens=10_000,
    )

    assert context.payload["session_memory"] == [
        {
            "kind": "untrusted_session_message",
            "role": "user",
            "content": "keep-current",
        }
    ]
    assert context.compression_metrics["budget_truncated_session_memory"] == 1
    assert context.estimated_tokens <= context.input_budget_tokens


def test_working_memory_uses_the_injected_meter_for_cjk_messages() -> None:
    meter = TokenMeter(tokenizer=lambda text: len(text))
    memory = WorkingMemory(max_tokens=4, token_meter=meter)

    memory.add_message("tool", "中文" * 20, importance=0.1)
    memory.add_message("user", "保留", importance=2.0)

    assert "保留" in memory.render()
    assert "中文" * 20 not in memory.render()
