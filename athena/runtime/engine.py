"""Deterministic decision adapter used when no external model is configured."""

from __future__ import annotations

import re
from typing import Protocol

from .models import ContextSnapshot, Decision, DecisionKind

_ABSOLUTE_PATH = re.compile(r"(?:[A-Za-z]:[\\/]|/)[^\s'\"，。；、]+")


class DecisionEngine(Protocol):
    def decide(self, context: ContextSnapshot) -> Decision: ...


class DemoDecisionEngine:
    """A fixed four-step repository diagnosis for offline demonstrations.

    It models the public ReAct contract (one decision, one tool per Tick) but
    deliberately contains no hidden reasoning. A provider-backed engine can
    later replace this adapter at the same seam.
    """

    def decide(self, context: ContextSnapshot) -> Decision:
        goal = str(context.payload["task"]["goal"])
        working_state = context.payload["working_memory"]
        human_input = working_state.get("human_input")
        evidence = context.payload["evidence_memory"]
        normalized_goal = goal.lower()
        if "ask human" in normalized_goal and not human_input:
            return Decision(
                kind=DecisionKind.ASK_HUMAN,
                reason_code="DEMO_REQUIRES_OPERATOR_INPUT",
                response="请提供失败的测试名称或错误信息。",
            )
        if "fail runtime" in normalized_goal:
            return Decision(
                kind=DecisionKind.FAIL,
                reason_code="DEMO_PERMANENT_FAILURE",
                response="离线演示被要求进入永久失败状态。",
            )
        if not evidence:
            outside_path = _ABSOLUTE_PATH.search(goal)
            if outside_path is not None:
                return Decision(
                    kind=DecisionKind.TOOL_CALL,
                    reason_code="REPOSITORY_SCOPE_CHECK",
                    tool_name="read_file_range",
                    arguments={"relative_path": outside_path.group(0)},
                )
            return Decision(
                kind=DecisionKind.TOOL_CALL,
                reason_code="DEMO_CODE_DIAGNOSIS",
                tool_name="search_code",
                arguments={"query": "discounted_price_cents"},
            )
        if len(evidence) == 1:
            return Decision(
                kind=DecisionKind.TOOL_CALL,
                reason_code="INSPECT_SOURCE_IMPLEMENTATION",
                tool_name="read_file_range",
                arguments={"relative_path": "pricing.py", "start_line": 1, "end_line": 80},
            )
        if len(evidence) == 2:
            return Decision(
                kind=DecisionKind.TOOL_CALL,
                reason_code="VERIFY_REPRODUCIBLE_FAILURE",
                tool_name="run_test",
                arguments={"relative_path": "check_context_pressure.py"},
            )
        return Decision(
            kind=DecisionKind.FINAL,
            reason_code="EVIDENCE_SUFFICIENT",
            response="已根据已记录的只读工具证据完成代码诊断。",
        )
