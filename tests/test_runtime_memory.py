"""Focused contracts for the V1 four-layer Runtime memory module."""

from __future__ import annotations

import json
from inspect import signature

import pytest

from athena.runtime import AgentTask, Artifact, Evidence, WorkingState
from athena.runtime.memory import (
    EvaluatedSkill,
    InMemorySkillRetrievalAdapter,
    MemoryBudget,
    MemoryCheckpoint,
    MemoryLayer,
    PendingToolPair,
    RunningSummary,
    SkillEvaluationState,
)
from athena.runtime.models import utc_now


def _task() -> AgentTask:
    return AgentTask.create(
        goal="Diagnose the checkout total calculation failure",
        repository_root="/controlled/repository",
    )


def _checkpoint(
    *, summary: RunningSummary, tick_sequence: int = 0
) -> MemoryCheckpoint:
    return MemoryCheckpoint(
        tick_sequence=tick_sequence,
        working_state=WorkingState(
            plan=("Inspect the price calculation", "Run the focused test"),
            pending_items=("Confirm the rounding rule",),
            evidence_ids=("evidence_price",),
        ),
        constraints=(
            "Remain inside the repository root.",
            "Use read-only tools only.",
        ),
        running_summary=summary,
        unresolved_tool_pairs=(
            PendingToolPair(
                call_id="call_read_price",
                tool_name="read_file_range",
                request_summary="Read the calculate_total implementation.",
            ),
        ),
    )


def _evidence() -> tuple[Evidence, ...]:
    return (
        Evidence(
            evidence_id="evidence_price",
            task_id="task_memory",
            artifact_id="artifact_price",
            source="tool:read_file_range",
            summary="The implementation rounds each line before adding the totals.",
            created_at=utc_now(),
        ),
    )


def test_forced_compaction_preserves_runtime_anchors_and_artifact_references() -> None:
    task = _task()
    raw_artifact = Artifact(
        artifact_id="artifact_price",
        task_id=task.task_id,
        tick_id="tick_memory",
        tool_name="read_file_range",
        content={"raw": "RAW_ARTIFACT_CONTENT_MUST_NOT_REACH_THE_PROMPT"},
        content_hash="not-used-by-memory-layer",
        created_at=utc_now(),
    )
    summary = RunningSummary(
        completed_facts=tuple(
            f"Historical fact {index}: " + "detail " * 30 for index in range(40)
        ),
        open_questions=("Does rounding happen before the total is calculated?",),
        next_actions=("Read the implementation and compare it with the test.",),
    )

    snapshot = MemoryLayer().compile(
        task=task,
        checkpoint=_checkpoint(summary=summary, tick_sequence=7),
        evidence=_evidence(),
        budget=MemoryBudget(
            model_window_tokens=1_024,
            output_reserve_tokens=128,
            safety_margin_tokens=64,
        ),
    )

    payload = snapshot.payload
    rendered = json.dumps(payload, ensure_ascii=False)

    assert payload["schema_version"] == "runtime.memory.v1"
    assert payload["task"]["goal"] == task.goal
    assert payload["task"]["constraints"] == [
        "Remain inside the repository root.",
        "Use read-only tools only.",
    ]
    assert payload["working_memory"]["unresolved_tool_pairs"] == [
        {
            "call_id": "call_read_price",
            "tool_name": "read_file_range",
            "request_summary": "Read the calculate_total implementation.",
            "result_summary": None,
        }
    ]
    assert payload["working_memory"]["pinned_evidence_ids"] == ["evidence_price"]
    assert payload["evidence_memory"] == [
        {
            "evidence_id": "evidence_price",
            "artifact_id": "artifact_price",
            "source": "tool:read_file_range",
            "summary": "The implementation rounds each line before adding the totals.",
        }
    ]
    assert payload["memory_governance"]["forced_compaction"] is True
    assert payload["memory_governance"]["artifact_content_policy"] == "references_only"
    assert snapshot.input_budget_tokens == 832
    assert snapshot.output_reserve_tokens == 128
    assert snapshot.tick_sequence == 7
    assert raw_artifact.content["raw"] not in rendered
    assert snapshot.compacted is True
    assert snapshot.compaction_count == 1
    assert snapshot.estimated_input_tokens <= snapshot.input_budget_tokens


def test_summary_candidate_is_prepared_at_seventy_five_percent_without_forcing(
) -> None:
    task = _task()
    checkpoint = _checkpoint(
        summary=RunningSummary(
            completed_facts=("Relevant history: " + "fact " * 350,),
            next_actions=("Run the focused test after inspection.",),
        )
    )

    snapshot = MemoryLayer().compile(
        task=task,
        checkpoint=checkpoint,
        evidence=_evidence(),
        budget=MemoryBudget(
            model_window_tokens=1_024,
            output_reserve_tokens=128,
            safety_margin_tokens=64,
        ),
    )

    governance = snapshot.payload["memory_governance"]
    assert governance["summary_candidate"] is True
    assert governance["forced_compaction"] is False
    assert 0.75 <= governance["input_utilization"] < 0.90
    assert snapshot.compacted is False


def test_skill_retrieval_never_injects_unevaluated_candidates() -> None:
    task = _task()
    adapter = InMemorySkillRetrievalAdapter(
        skills=(
            EvaluatedSkill(
                skill_id="skill_test_first",
                title="Test-first diagnosis",
                procedure_summary="Run the focused test before changing code.",
                evaluation_state=SkillEvaluationState.APPROVED,
                source_references=("evaluation:shadow-14",),
            ),
            EvaluatedSkill(
                skill_id="skill_unreviewed",
                title="Unreviewed candidate",
                procedure_summary="This text must not be injected.",
                evaluation_state=SkillEvaluationState.CANDIDATE,
            ),
        )
    )

    snapshot = MemoryLayer(skill_retrieval=adapter).compile(
        task=task,
        checkpoint=_checkpoint(summary=RunningSummary()),
        evidence=_evidence(),
        budget=MemoryBudget(model_window_tokens=4_096, output_reserve_tokens=512),
    )

    assert snapshot.payload["skill_memory"] == [
        {
            "skill_id": "skill_test_first",
            "title": "Test-first diagnosis",
            "procedure_summary": "Run the focused test before changing code.",
            "source_references": ["evaluation:shadow-14"],
        }
    ]


def test_memory_layer_exposes_the_stable_v1_context_compiler_contract() -> None:
    parameters = list(signature(MemoryLayer.compile).parameters)

    assert parameters == ["self", "task", "checkpoint", "evidence", "budget"]
    snapshot = MemoryLayer().compile(
        task=_task(),
        checkpoint=_checkpoint(summary=RunningSummary()),
        evidence=_evidence(),
        budget=MemoryBudget(model_window_tokens=4_096, output_reserve_tokens=512),
    )

    assert set(snapshot.payload) == {
        "schema_version",
        "task",
        "working_memory",
        "running_summary",
        "evidence_memory",
        "skill_memory",
        "memory_governance",
    }


def test_checkpoint_rejects_an_invalid_tick_sequence() -> None:
    with pytest.raises(ValueError, match="tick_sequence"):
        MemoryCheckpoint(tick_sequence=-1)
