"""LLM-backed, structured decision adapter for the Agent Runtime.

The Runtime persists only a public ``Decision``.  This module deliberately
keeps provider text, repair prompts, and any parsing failure out of that
aggregate.  A malformed provider response therefore cannot become a tool
effect.
"""

from __future__ import annotations

import asyncio
import json
import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from athena.infra.llm import LLMClient, LLMMessage, LLMResponse
from athena.infra.model_router import ModelRouter

from .models import ContextSnapshot, Decision, DecisionKind

_REASON_CODE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
_QUALITY_PURPOSES = frozenset({"final_report", "repair_plan", "safety_review"})
_ECONOMY_BUDGET_MODES = frozenset({"ECONOMY", "CONVERGE", "FINALIZE"})
_MAX_REPAIR_OUTPUT_CHARS = 12_000


class DecisionFormatError(ValueError):
    """Raised when a provider response is not the Runtime Decision contract."""


@dataclass(frozen=True)
class DecisionRoutingRecord:
    """Explainable model-routing and usage projection for one decision call.

    Mainline integration persists this object with the Runtime ``Usage``
    record.  Keeping it here lets the decision adapter remain usable before
    the durable store is wired in.
    """

    purpose: str
    budget_mode: str
    preferred_tier: str
    selected_tier: str
    complexity_score: float
    route_reason: str
    model: str | None
    estimated_input_tokens: int
    actual_input_tokens: int
    actual_output_tokens: int
    repair_attempted: bool
    fallback_reason: str | None = None

    @property
    def actual_tokens(self) -> int:
        return self.actual_input_tokens + self.actual_output_tokens


class LLMDecisionEngine:
    """Use ``ModelRouter`` and strict JSON to produce one public Decision.

    ``DecisionEngine`` is currently synchronous because ``AgentRuntime`` is
    worker-oriented.  The provider clients are async, so ``decide`` bridges
    exactly that boundary.  When called from an async request handler it uses
    a short-lived helper thread rather than nesting an event loop.
    """

    def __init__(
        self,
        model_router: ModelRouter,
        *,
        purpose: str = "react_decision",
        max_repair_output_chars: int = _MAX_REPAIR_OUTPUT_CHARS,
    ) -> None:
        if not purpose.strip():
            raise ValueError("purpose must be a non-empty string")
        if max_repair_output_chars < 1:
            raise ValueError("max_repair_output_chars must be positive")
        self._model_router = model_router
        self._purpose = purpose.strip()
        self._max_repair_output_chars = max_repair_output_chars
        self._last_routing: DecisionRoutingRecord | None = None

    @property
    def last_routing(self) -> DecisionRoutingRecord | None:
        """Return the latest routing projection without exposing provider text."""

        return self._last_routing

    def decide(self, context: ContextSnapshot) -> Decision:
        """Return a valid Decision, repairing provider formatting at most once."""

        purpose = self._purpose
        budget_mode = self._budget_mode(context)
        profile = context.payload.get("task", {}).get("profile")
        preferred_tier = self._preferred_tier(budget_mode, purpose, profile)
        messages = self._decision_messages(context)
        client, selected_tier, complexity_score = self._model_router.route(
            messages, preference=preferred_tier
        )
        route_reason = self._route_reason(
            purpose=purpose,
            budget_mode=budget_mode,
            preferred_tier=preferred_tier,
            selected_tier=selected_tier,
            complexity_score=complexity_score,
        )

        responses: list[LLMResponse] = []
        repair_attempted = False
        try:
            response = self._complete(client, messages)
            responses.append(response)
            decision = parse_decision_json(
                response.content,
                allowed_tool_names=self._selected_tool_names(context),
            )
        except Exception as first_error:
            repair_attempted = True
            decision = self._repair_or_fallback(
                client=client,
                context=context,
                provider_output=(responses[-1].content if responses else ""),
                first_error=first_error,
                responses=responses,
            )

        self._last_routing = self._routing_record(
            purpose=purpose,
            budget_mode=budget_mode,
            preferred_tier=preferred_tier,
            selected_tier=selected_tier,
            complexity_score=complexity_score,
            route_reason=route_reason,
            responses=responses,
            context=context,
            repair_attempted=repair_attempted,
            fallback_reason=(
                decision.reason_code
                if decision.kind is DecisionKind.ASK_HUMAN
                and decision.reason_code.startswith("LLM_")
                else None
            ),
        )
        return decision

    def _repair_or_fallback(
        self,
        *,
        client: LLMClient,
        context: ContextSnapshot,
        provider_output: str,
        first_error: Exception,
        responses: list[LLMResponse],
    ) -> Decision:
        """Spend one bounded repair attempt before asking the operator."""

        try:
            repaired = self._complete(
                client,
                self._repair_messages(context, provider_output),
            )
            responses.append(repaired)
            return parse_decision_json(
                repaired.content,
                allowed_tool_names=self._selected_tool_names(context),
            )
        except Exception:
            # The persisted Decision deliberately does not include an exception
            # message: provider failures can contain credentials or internals.
            reason_code = (
                "LLM_DECISION_FORMAT_INVALID"
                if isinstance(first_error, DecisionFormatError)
                else "LLM_DECISION_UNAVAILABLE"
            )
            return Decision(
                kind=DecisionKind.ASK_HUMAN,
                reason_code=reason_code,
                response="模型决策未通过结构校验，请补充目标或稍后重试。",
            )

    def _decision_messages(self, context: ContextSnapshot) -> list[LLMMessage]:
        payload = self._model_payload(context)
        contract = self._decision_contract(self._selected_tool_names(context))
        return [
            LLMMessage(
                role="system",
                content=(
                    "You are a constrained Agent Runtime decision engine.\n"
                    "Return exactly one JSON object and nothing else. Do not use "
                    "Markdown, explanations, hidden fields, absolute paths, or "
                    "provider-specific fields. reason_code must match "
                    "^[A-Z][A-Z0-9_]{0,63}$.\n"
                    "Use read-only tools to collect evidence. A non-zero test "
                    "exit code is diagnostic evidence, not an Agent failure. "
                    "After sufficient evidence is collected, return a final "
                    "recommendation with a non-empty response; do not repeat a "
                    "successful tool call.\n"
                    f"Decision contract: {json.dumps(contract, ensure_ascii=True)}"
                ),
            ),
            LLMMessage(
                role="user",
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        ]

    def _repair_messages(
        self, context: ContextSnapshot, provider_output: str
    ) -> list[LLMMessage]:
        contract = self._decision_contract(self._selected_tool_names(context))
        payload = {
            "invalid_output": provider_output[: self._max_repair_output_chars],
            "decision_contract": contract,
        }
        return [
            LLMMessage(
                role="system",
                content=(
                    "Repair the invalid model output into exactly one JSON object "
                    "matching the supplied contract. Output JSON only. Do not "
                    "add tools, server fields, explanations, Markdown, or hidden "
                    "reasoning. reason_code must match "
                    "^[A-Z][A-Z0-9_]{0,63}$. A final decision must contain a "
                    "non-empty response string."
                ),
            ),
            LLMMessage(
                role="user",
                content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        ]

    @staticmethod
    def _model_payload(context: ContextSnapshot) -> dict[str, Any]:
        """Project only model-visible data; server values never enter the prompt."""

        payload = context.payload
        task = payload.get("task", {})
        working_state = payload.get("working_state", payload.get("working_memory", {}))
        running_summary = payload.get("running_summary", {})
        evidence = payload.get("evidence", payload.get("evidence_memory", []))
        schemas = context.tool_schemas or tuple(payload.get("selected_tool_schemas", []))
        return {
            "task": {
                "goal": task.get("goal", ""),
                "budget_mode": task.get("budget_mode", "NORMAL"),
            },
            "working_state": {
                "plan": working_state.get("plan", []),
                "pending_items": working_state.get("pending_items", []),
                "evidence_ids": working_state.get("evidence_ids", []),
                "running_summary": (
                    working_state.get("running_summary", running_summary)
                ),
                "human_input": working_state.get("human_input"),
            },
            "evidence": evidence,
            "selected_tool_schemas": list(schemas)[:3],
            "recent_events": payload.get("recent_events", []),
        }

    @staticmethod
    def _budget_mode(context: ContextSnapshot) -> str:
        task = context.payload.get("task", {})
        value = task.get("budget_mode", "NORMAL") if isinstance(task, Mapping) else "NORMAL"
        return value if isinstance(value, str) and value else "NORMAL"

    @staticmethod
    def _selected_tool_names(context: ContextSnapshot) -> frozenset[str]:
        schemas = context.tool_schemas or context.payload.get("selected_tool_schemas", [])
        if not isinstance(schemas, list):
            return frozenset()
        return frozenset(
            item["name"]
            for item in schemas[:3]
            if isinstance(item, Mapping) and isinstance(item.get("name"), str)
        )

    @staticmethod
    def _decision_contract(tool_names: frozenset[str]) -> dict[str, Any]:
        return {
            "reason_code_pattern": "^[A-Z][A-Z0-9_]{0,63}$",
            "final_response": "required and non-empty",
            "examples": {
                "tool_call": {
                    "kind": "tool_call",
                    "reason_code": "COLLECT_EVIDENCE",
                    "tool_name": "<one_allowed_tool>",
                    "arguments": {},
                },
                "final": {
                    "kind": "final",
                    "reason_code": "DIAGNOSIS_COMPLETE",
                    "response": "Evidence is sufficient; provide the recommendation.",
                },
            },
            "one_of": [
                {
                    "kind": "tool_call",
                    "required": ["kind", "reason_code", "tool_name", "arguments"],
                    "tool_names": sorted(tool_names),
                },
                {
                    "kind": "final",
                    "required": ["kind", "reason_code", "response"],
                },
                {
                    "kind": "ask_human",
                    "required": ["kind", "reason_code", "response"],
                },
                {
                    "kind": "fail",
                    "required": ["kind", "reason_code", "response"],
                },
            ]
        }

    @staticmethod
    def _preferred_tier(
        budget_mode: str, purpose: str, profile: str | None = None
    ) -> str:
        if purpose in _QUALITY_PURPOSES:
            return "quality"
        if profile == "complex":
            return "quality"
        if profile == "simple":
            return "economy"
        if budget_mode in _ECONOMY_BUDGET_MODES:
            return "economy"
        return "adaptive"

    @staticmethod
    def _route_reason(
        *,
        purpose: str,
        budget_mode: str,
        preferred_tier: str,
        selected_tier: str,
        complexity_score: float,
    ) -> str:
        return (
            f"purpose={purpose};budget_mode={budget_mode};"
            f"preference={preferred_tier};complexity={complexity_score:.2f};"
            f"selected={selected_tier}"
        )

    def _routing_record(
        self,
        *,
        purpose: str,
        budget_mode: str,
        preferred_tier: str,
        selected_tier: str,
        complexity_score: float,
        route_reason: str,
        responses: Sequence[LLMResponse],
        context: ContextSnapshot,
        repair_attempted: bool,
        fallback_reason: str | None,
    ) -> DecisionRoutingRecord:
        actual_input = sum(self._usage_value(item.usage, "prompt_tokens", "input_tokens") for item in responses)
        actual_output = sum(
            self._usage_value(item.usage, "completion_tokens", "output_tokens")
            for item in responses
        )
        return DecisionRoutingRecord(
            purpose=purpose,
            budget_mode=budget_mode,
            preferred_tier=preferred_tier,
            selected_tier=selected_tier,
            complexity_score=complexity_score,
            route_reason=route_reason,
            model=(responses[-1].model if responses else None),
            estimated_input_tokens=context.estimated_input_tokens,
            actual_input_tokens=actual_input,
            actual_output_tokens=actual_output,
            repair_attempted=repair_attempted,
            fallback_reason=fallback_reason,
        )

    @staticmethod
    def _usage_value(usage: Mapping[str, int], *names: str) -> int:
        for name in names:
            value = usage.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                return value
        return 0

    @staticmethod
    def _complete(client: LLMClient, messages: Sequence[LLMMessage]) -> LLMResponse:
        coroutine = client.complete(messages)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)

        # AgentRuntime is normally called by a worker thread.  This bridge
        # keeps a synchronous Engine usable in a test or a legacy async caller
        # without trying to nest ``asyncio.run`` in its active event loop.
        response: list[LLMResponse] = []
        error: list[BaseException] = []

        def run() -> None:
            try:
                response.append(asyncio.run(coroutine))
            except BaseException as exc:  # propagated below on the caller thread
                error.append(exc)

        thread = threading.Thread(target=run, name="runtime-llm-decision", daemon=True)
        thread.start()
        thread.join()
        if error:
            raise error[0]
        if not response:
            raise RuntimeError("LLM completion returned no response")
        return response[0]


def parse_decision_json(
    content: str, *, allowed_tool_names: frozenset[str] | None = None
) -> Decision:
    """Parse exactly one Decision object and reject unknown fields or types."""

    if not isinstance(content, str) or not content.strip():
        raise DecisionFormatError("Decision content must be a non-empty JSON object")
    try:
        value = json.loads(
            content,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_non_standard_constant,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise DecisionFormatError("Decision content must be valid JSON") from exc
    if not isinstance(value, dict):
        raise DecisionFormatError("Decision must be a JSON object")

    kind_value = value.get("kind")
    reason_code = value.get("reason_code")
    if not isinstance(kind_value, str):
        raise DecisionFormatError("Decision kind must be a string")
    try:
        kind = DecisionKind(kind_value)
    except ValueError as exc:
        raise DecisionFormatError("Decision kind is unsupported") from exc
    if not isinstance(reason_code, str) or not _REASON_CODE.fullmatch(reason_code):
        raise DecisionFormatError("Decision reason_code is invalid")

    if kind is DecisionKind.TOOL_CALL:
        _require_exact_keys(value, {"kind", "reason_code", "tool_name", "arguments"})
        tool_name = value.get("tool_name")
        arguments = value.get("arguments")
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise DecisionFormatError("tool_call requires a non-empty tool_name")
        if allowed_tool_names is not None and tool_name not in allowed_tool_names:
            raise DecisionFormatError("tool_call references a tool outside this Tick")
        if not isinstance(arguments, dict) or not _is_json_value(arguments):
            raise DecisionFormatError("tool_call arguments must be a JSON object")
        return Decision(
            kind=kind,
            reason_code=reason_code,
            tool_name=tool_name,
            arguments=arguments,
        )

    _require_exact_keys(value, {"kind", "reason_code", "response"})
    response = value.get("response")
    if not isinstance(response, str) or not response.strip():
        raise DecisionFormatError(f"{kind.value} requires a non-empty response")
    return Decision(kind=kind, reason_code=reason_code, response=response)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise DecisionFormatError(f"duplicate Decision field: {key}")
        value[key] = item
    return value


def _reject_non_standard_constant(value: str) -> None:
    raise DecisionFormatError(f"non-standard JSON constant: {value}")


def _require_exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise DecisionFormatError("Decision fields do not match its declared kind")


def _is_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int, float)):
        return True
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_json_value(item) for key, item in value.items())
    return False
