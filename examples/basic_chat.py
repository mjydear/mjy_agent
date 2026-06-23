"""Run Athena Agent with a deterministic fake LLM for local smoke testing."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from athena.agent import ReActAgent
from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry
from athena.tools.builtin.basic import register_basic_tools


class DemoLLMClient(LLMClient):
    """Deterministic LLM client for example execution without API keys."""

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """Return a final answer immediately."""
        return LLMResponse(
            content='{"thought":"Answer directly for the demo.","action":null,"action_input":{},"final_answer":"Athena MVP is running."}',
            model="demo",
        )


async def main() -> None:
    """Run the example agent."""
    registry = ToolRegistry()
    register_basic_tools(registry)
    agent = ReActAgent(
        llm_client=DemoLLMClient(),
        prompt_assembler=ContextAssembler(),
        tool_registry=registry,
        memory=WorkingMemory(),
    )
    response = await agent.run("Check the MVP status")
    print(response.answer)


if __name__ == "__main__":
    asyncio.run(main())
