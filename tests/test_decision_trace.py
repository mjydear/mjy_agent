"""Regression tests for redacted structured legacy traces."""

from __future__ import annotations

import pytest

from athena.agent import ReActAgent
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.memory import WorkingMemory
from athena.observability.decision_trace import StructuredTraceRecorder
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry


class _ScriptedLLM(LLMClient):
    def __init__(self) -> None:
        self._responses = iter(
            (
                '{"thought":"api_key=secret", "action":"echo", '
                '"action_input":{"text":"done", "api_token":"secret"}}',
            )
        )

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        return LLMResponse(
            content=next(self._responses),
            model="scripted",
            usage={"prompt_tokens": 11, "completion_tokens": 7},
        )


@pytest.mark.asyncio
async def test_legacy_trace_is_redacted_and_does_not_change_tool_execution() -> None:
    registry = ToolRegistry()

    @registry.register
    def echo(text: str, api_token: str = "") -> str:
        """Echo the requested text."""
        return text

    recorder = StructuredTraceRecorder()
    agent = ReActAgent(
        llm_client=_ScriptedLLM(),
        prompt_assembler=ContextAssembler(),
        tool_registry=registry,
        memory=WorkingMemory(),
        trace_recorder=recorder,
    )

    response = await agent.run("please echo")

    assert response.answer == "done"
    assert response.steps == [
        "Thought: api_key=secret",
        "Observation: done",
    ]
    events = recorder.events_for(agent.last_trace_run_id or "")
    assert [event.event_type for event in events] == [
        "agent.started",
        "llm.completed",
        "decision.recorded",
        "tool.started",
        "tool.finished",
        "agent.finished",
    ]
    serialized = str([event.payload_redacted for event in events])
    assert "api_key" not in serialized
    assert "[REDACTED]" in serialized
    assert "Observation" not in serialized


def test_task_projection_preserves_sequence_and_tenant_boundary() -> None:
    recorder = StructuredTraceRecorder()
    run_id = recorder.start_run()
    recorder.record_decision(
        run_id,
        step=1,
        action="k8s.logs.read",
        arguments={"namespace": "payment"},
    )

    events = recorder.project_task(run_id, task_id="task-1", tenant_id="tenant-a")

    assert [event.sequence for event in events] == [1, 2]
    assert all(event.task_id == "task-1" for event in events)
    assert all(event.tenant_id == "tenant-a" for event in events)
    assert events[-1].reason_code == "LEGACY_REACT_DECISION"
