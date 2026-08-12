"""Focused tests for the V1 structured LLM DecisionEngine adapter."""

from __future__ import annotations

import json

from athena.infra.llm import LLMMessage, LLMResponse
from athena.infra.model_router import ModelRouter
from athena.runtime.llm_engine import LLMDecisionEngine, parse_decision_json
from athena.runtime.models import ContextSnapshot, DecisionKind


class _QueuedClient:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.messages: list[list[LLMMessage]] = []

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        self.messages.append(list(messages))
        return LLMResponse(
            content=self._responses.pop(0),
            model="stub-model",
            usage={"prompt_tokens": 13, "completion_tokens": 7},
        )


class _UnavailableThenValidClient(_QueuedClient):
    def __init__(self, response: str) -> None:
        super().__init__(response)
        self._failed = False

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        if not self._failed:
            self._failed = True
            raise ConnectionError("provider unavailable")
        return await super().complete(messages)


def _context(*, goal: str = "诊断 pricing.py 的测试失败", schemas: int = 1) -> ContextSnapshot:
    selected = [
        {
            "name": f"tool_{index}",
            "description": "只读测试工具",
            "input_schema": {"type": "object", "properties": {}},
            "readonly": True,
        }
        for index in range(schemas)
    ]
    return ContextSnapshot(
        task_id="task-1",
        tick_sequence=1,
        payload={
            "task": {"goal": goal, "budget_mode": "NORMAL", "repository_root": "D:/secret"},
            "working_state": {"plan": [], "pending_items": [], "evidence_ids": []},
            "evidence": [],
            "selected_tool_schemas": selected,
            "recent_events": [],
        },
        estimated_input_tokens=101,
        input_budget_tokens=1_000,
        output_reserve_tokens=128,
        compacted=False,
        omitted_event_count=0,
    )


def test_llm_engine_parses_strict_tool_decision_and_records_route() -> None:
    client = _QueuedClient(
        json.dumps(
            {
                "kind": "tool_call",
                "reason_code": "INSPECT_SOURCE",
                "tool_name": "tool_0",
                "arguments": {"relative_path": "pricing.py"},
            }
        )
    )
    engine = LLMDecisionEngine(ModelRouter(client, client))

    decision = engine.decide(_context())

    assert decision.kind is DecisionKind.TOOL_CALL
    assert decision.arguments == {"relative_path": "pricing.py"}
    assert engine.last_routing is not None
    assert engine.last_routing.actual_tokens == 20
    assert engine.last_routing.purpose == "react_decision"
    assert "budget_mode=NORMAL" in engine.last_routing.route_reason


def test_llm_engine_repairs_once_and_never_persists_malformed_output() -> None:
    client = _QueuedClient(
        "```json\n{not valid}\n```",
        json.dumps(
            {
                "kind": "final",
                "reason_code": "EVIDENCE_SUFFICIENT",
                "response": "已完成诊断。",
            }
        ),
    )
    engine = LLMDecisionEngine(ModelRouter(client, client))

    decision = engine.decide(_context())

    assert decision.kind is DecisionKind.FINAL
    assert len(client.messages) == 2
    assert engine.last_routing is not None
    assert engine.last_routing.repair_attempted is True
    assert engine.last_routing.actual_tokens == 40


def test_llm_engine_asks_operator_after_one_invalid_repair() -> None:
    client = _QueuedClient("not json", '{"kind":"tool_call"}')
    engine = LLMDecisionEngine(ModelRouter(client, client))

    decision = engine.decide(_context())

    assert decision.kind is DecisionKind.ASK_HUMAN
    assert decision.reason_code == "LLM_DECISION_FORMAT_INVALID"
    assert len(client.messages) == 2


def test_llm_engine_records_repair_after_provider_failure() -> None:
    client = _UnavailableThenValidClient(
        json.dumps(
            {
                "kind": "ask_human",
                "reason_code": "NEEDS_CONTEXT",
                "response": "请提供失败日志。",
            }
        )
    )
    engine = LLMDecisionEngine(ModelRouter(client, client))

    decision = engine.decide(_context())

    assert decision.reason_code == "NEEDS_CONTEXT"
    assert engine.last_routing is not None
    assert engine.last_routing.repair_attempted is True


def test_llm_engine_limits_visible_schemas_and_strict_parser_rejects_extra_fields() -> None:
    client = _QueuedClient(
        json.dumps(
            {
                "kind": "final",
                "reason_code": "EVIDENCE_SUFFICIENT",
                "response": "完成。",
            }
        )
    )
    engine = LLMDecisionEngine(ModelRouter(client, client), purpose="final_report")

    engine.decide(_context(schemas=5))

    visible_payload = json.loads(client.messages[0][1].content)
    assert len(visible_payload["selected_tool_schemas"]) == 3
    assert "repository_root" not in visible_payload["task"]
    assert engine.last_routing is not None
    assert engine.last_routing.preferred_tier == "quality"
    try:
        parse_decision_json(
            '{"kind":"final","reason_code":"DONE","response":"ok","trace":"no"}'
        )
    except ValueError as exc:
        assert "fields" in str(exc)
    else:
        raise AssertionError("strict parser accepted an unknown Decision field")
