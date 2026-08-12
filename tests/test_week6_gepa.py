"""Tests for GEPA self-evolution modules."""

from __future__ import annotations

import pytest

from athena.learning import (
    ComplexityEvaluator,
    SkillGenerator,
    SkillValidator,
    TraceEvent,
)
from athena.tools import SecuritySandbox


def test_complexity_evaluator_scores_tool_diversity() -> None:
    events = (
        TraceEvent(name="agent.step", run_id="r1", payload={"detail": "plan"}),
        TraceEvent(name="tool.call", run_id="r1", payload={"tool": "git_status"}),
        TraceEvent(name="tool.call", run_id="r1", payload={"tool": "read_text_file"}),
    )

    score = ComplexityEvaluator(skill_threshold=0.1).evaluate(
        events, task_difficulty=0.8
    )

    assert score.tool_count == 2
    assert score.should_generate_skill


@pytest.mark.asyncio
async def test_skill_generation_and_validation() -> None:
    events = (
        TraceEvent(name="agent.step", run_id="r2", payload={"detail": "inspect logs"}),
    )
    complexity = ComplexityEvaluator(skill_threshold=0.1).evaluate(
        events, task_difficulty=0.8
    )
    generated = SkillGenerator().build_skill(
        "Inspect Logs", events, complexity, success=True
    )
    validation = await SkillValidator(
        SecuritySandbox(), acceptance_threshold=0.5
    ).validate(generated.skill, simulation_runs=1)

    assert generated.skill.name == "inspect_logs"
    assert validation.accepted
