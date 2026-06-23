"""Tests for the MVP ReAct executor."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from athena.agent import ReActAgent
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry


class ScriptedLLMClient(LLMClient):
    """LLM test double returning scripted responses."""

    def __init__(self, responses: Sequence[str]) -> None:
        """Initialize with ordered responses."""
        self.responses = list(responses)

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """Return the next scripted response."""
        content = self.responses.pop(0)
        return LLMResponse(content=content, model="scripted")


@pytest.mark.asyncio
async def test_react_agent_uses_tool_then_returns_answer() -> None:
    """Agent should execute a tool and continue to final answer."""
    registry = ToolRegistry()

    @registry.register
    def echo(text: str) -> str:
        """Echo text."""
        return text

    llm_client = ScriptedLLMClient(
        responses=(
            '{"thought":"Use echo.","action":"echo","action_input":{"text":"done"},"final_answer":null}',
            '{"thought":"Observed result.","action":null,"action_input":{},"final_answer":"done"}',
        )
    )
    agent = ReActAgent(
        llm_client=llm_client,
        prompt_assembler=ContextAssembler(),
        tool_registry=registry,
        memory=WorkingMemory(),
        max_steps=3,
    )

    response = await agent.run("please echo")

    assert response.answer == "done"
    assert "Observation: done" in response.steps
