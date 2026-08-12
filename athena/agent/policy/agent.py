"""Structured one-action policy decisions for governed workflows."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

from athena.agent.context.manager import DecisionContext
from athena.agent.policy.contracts import ActionDecision
from athena.infra.llm import LLMClient, LLMMessage


class PolicyDecisionError(ValueError):
    """The model or fallback did not produce an allowed ActionDecision."""


DecisionFallback = Callable[
    [DecisionContext], ActionDecision | Awaitable[ActionDecision]
]


class PolicyAgent:
    """Choose one already-authorized action; execution remains outside this class."""

    def __init__(
        self,
        llm_client: LLMClient | None = None,
        *,
        fallback: DecisionFallback | None = None,
    ) -> None:
        self._llm_client = llm_client
        self._fallback = fallback

    async def decide(self, context: DecisionContext) -> ActionDecision:
        if self._llm_client is None:
            return await self._fallback_decision(context)
        messages = [
            LLMMessage(
                role="system",
                content=(
                    "Return one JSON object with action, arguments, reason_code, and "
                    "optional confidence. Select only available_actions."
                ),
            ),
            LLMMessage(
                role="user", content=json.dumps(context.payload, ensure_ascii=False)
            ),
        ]
        for attempt in range(2):
            try:
                response = await self._llm_client.complete(messages)
            except Exception:
                return await self._fallback_decision(context)
            try:
                decision = self._parse(response.content, context)
                return decision
            except PolicyDecisionError:
                if attempt:
                    break
                messages.append(
                    LLMMessage(
                        role="user",
                        content="Previous output was invalid. Return only the required JSON object.",
                    )
                )
        return await self._fallback_decision(context)

    async def _fallback_decision(self, context: DecisionContext) -> ActionDecision:
        if self._fallback is None:
            raise PolicyDecisionError("no structured policy decision is available")
        result = self._fallback(context)
        if hasattr(result, "__await__"):
            result = await result
        self._validate(result, context)
        return result

    @staticmethod
    def _parse(content: str, context: DecisionContext) -> ActionDecision:
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise TypeError("decision must be an object")
            decision = ActionDecision(
                action=str(payload["action"]),
                arguments=dict(payload.get("arguments") or {}),
                reason_code=str(payload["reason_code"]),
                confidence=payload.get("confidence"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PolicyDecisionError("invalid structured decision") from exc
        PolicyAgent._validate(decision, context)
        return decision

    @staticmethod
    def _validate(decision: ActionDecision, context: DecisionContext) -> None:
        if decision.action not in context.available_actions:
            raise PolicyDecisionError("decision action is not available")
