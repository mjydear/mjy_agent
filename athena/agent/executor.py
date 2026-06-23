"""Self-contained ReAct execution loop for the MVP agent."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import cast

from athena.agent.base import AgentResponse
from athena.exceptions import AgentError, AthenaError, ErrorCode
from athena.infra.llm import LLMClient, LLMMessage
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolCall, ToolRegistry
from athena.types import JSONValue
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReActDecision:
    """Structured decision parsed from one LLM step."""

    thought: str = ""
    action: str | None = None
    action_input: dict[str, JSONValue] = field(default_factory=dict)
    final_answer: str | None = None


@dataclass
class ReActAgent:
    """Minimal ReAct agent using Thought-Action-Observation iterations."""

    llm_client: LLMClient
    prompt_assembler: ContextAssembler
    tool_registry: ToolRegistry
    memory: WorkingMemory
    max_steps: int = 6

    def __post_init__(self) -> None:
        """Validate agent construction arguments."""
        if self.max_steps <= 0:
            raise AgentError(ErrorCode.AGENT_EXECUTION_FAILED, "max_steps must be positive")

    async def run(self, query: str) -> AgentResponse:
        """Run the ReAct loop for a user query.

        Args:
            query: User task.

        Returns:
            Final agent response with trace snippets.

        Raises:
            AgentError: If the loop cannot complete.
        """
        if not query.strip():
            raise AgentError(ErrorCode.AGENT_EXECUTION_FAILED, "Query must not be empty")
        self.memory.add_message("user", query, importance=2.0)
        scratchpad = ""
        steps: list[str] = []
        try:
            for step_index in range(1, self.max_steps + 1):
                prompt = self.prompt_assembler.build_prompt(
                    query=query,
                    memory=self.memory,
                    tools=self.tool_registry,
                    scratchpad=scratchpad,
                )
                response = await self.llm_client.complete([LLMMessage(role="user", content=prompt)])
                decision = self._parse_decision(response.content)
                logger.info("Agent step %s thought=%s action=%s", step_index, decision.thought, decision.action)
                steps.append(f"Thought: {decision.thought}")
                if decision.final_answer and decision.action is None:
                    self.memory.add_message("assistant", decision.final_answer, importance=2.0)
                    return AgentResponse(answer=decision.final_answer, steps=steps)
                if decision.action is None:
                    answer = decision.final_answer or response.content
                    self.memory.add_message("assistant", answer, importance=2.0)
                    return AgentResponse(answer=answer, steps=steps)
                tool_arguments = self._repair_tool_arguments(
                    action=decision.action,
                    arguments=decision.action_input,
                    query=query,
                )
                tool_result = await self.tool_registry.invoke(
                    ToolCall(name=decision.action, arguments=tool_arguments)
                )
                observation = tool_result.content if tool_result.success else str(tool_result.error)
                if tool_result.success and decision.action == "echo":
                    self.memory.add_message("assistant", observation, importance=2.0)
                    steps.append(f"Observation: {observation}")
                    return AgentResponse(answer=observation, steps=steps)
                scratchpad += (
                    f"\nStep {step_index}\n"
                    f"Thought: {decision.thought}\n"
                    f"Action: {decision.action}\n"
                    f"Observation: {observation}\n"
                )
                steps.append(f"Observation: {observation}")
            fallback = "I reached the maximum reasoning steps before producing a final answer."
            self.memory.add_message("assistant", fallback, importance=1.0)
            return AgentResponse(answer=fallback, steps=steps)
        except AgentError:
            raise
        except AthenaError:
            raise
        except Exception as exc:
            logger.exception("Agent execution failed")
            raise AgentError(ErrorCode.AGENT_EXECUTION_FAILED, str(exc)) from exc

    def _parse_decision(self, raw_content: str) -> ReActDecision:
        """Parse a JSON decision with a plain-text fallback."""
        try:
            loaded = json.loads(raw_content)
            if not isinstance(loaded, Mapping):
                return ReActDecision(final_answer=raw_content)
            payload = cast(Mapping[str, JSONValue], loaded)
            action_input = payload.get("action_input", {})
            arguments: dict[str, JSONValue] = {}
            if isinstance(action_input, Mapping):
                arguments = dict(action_input)
            action_value = payload.get("action")
            answer_value = payload.get("final_answer")
            return ReActDecision(
                thought=str(payload.get("thought", "")),
                action=str(action_value) if isinstance(action_value, str) else None,
                action_input=arguments,
                final_answer=str(answer_value) if isinstance(answer_value, str) else None,
            )
        except json.JSONDecodeError:
            logger.warning("LLM returned non-JSON content; using final-answer fallback")
            return ReActDecision(final_answer=raw_content)

    def _repair_tool_arguments(
        self,
        action: str,
        arguments: dict[str, JSONValue],
        query: str,
    ) -> dict[str, JSONValue]:
        """Repair simple missing tool arguments using the original user query."""
        if action == "echo" and "text" not in arguments:
            repaired = dict(arguments)
            repaired["text"] = query
            return repaired
        return arguments
