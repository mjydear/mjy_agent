"""Tests for multi-agent workflow and streaming."""

from __future__ import annotations

import pytest

from athena.agent import ExecutorAgent, PlannerAgent, ValidatorAgent, WorkflowEngine
from athena.agent.executor import ReActAgent
from athena.infra.llm import LLMMessage, LLMResponse
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry


@pytest.mark.asyncio
async def test_workflow_engine_runs_plan_execute_validate() -> None:
    engine = WorkflowEngine(PlannerAgent(), ExecutorAgent(), ValidatorAgent())

    response = await engine.run("collect metrics; validate fix")

    assert "Executed: collect metrics" in response.answer
    assert len(response.steps) == 2


class StaticLLM:
    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        return LLMResponse(
            content='{"thought":"done","action":null,"action_input":{},"final_answer":"ok"}',
            model="static",
        )


@pytest.mark.asyncio
async def test_react_agent_stream_run_emits_final_event() -> None:
    agent = ReActAgent(StaticLLM(), ContextAssembler(), ToolRegistry(), WorkingMemory(), max_steps=1)  # type: ignore[arg-type]

    events = [event async for event in agent.stream_run("hello")]

    assert events[0].event_type == "start"
    assert events[-1].event_type == "final"
    assert events[-1].content == "ok"
