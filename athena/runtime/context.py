"""Deterministic ContextSnapshot projection with a bounded event tail."""

from __future__ import annotations

import json
from typing import Any

from athena.infra.token_meter import TokenMeter

from .models import AgentTask, ContextSnapshot, Event, Evidence, WorkingState
from .tools import ToolDeclaration


class RuntimeContextCompiler:
    def __init__(
        self,
        *,
        model_window_tokens: int = 16_384,
        safety_margin_tokens: int = 1_024,
        token_meter: TokenMeter | None = None,
    ) -> None:
        if model_window_tokens <= 0 or safety_margin_tokens < 0:
            raise ValueError("context limits must be non-negative and non-zero")
        self._model_window_tokens = model_window_tokens
        self._safety_margin_tokens = safety_margin_tokens
        self._token_meter = token_meter or TokenMeter()

    def compile(
        self,
        *,
        task: AgentTask,
        tick_sequence: int,
        working_state: WorkingState,
        events: tuple[Event, ...],
        evidence: tuple[Evidence, ...],
        tools: tuple[ToolDeclaration, ...],
    ) -> ContextSnapshot:
        input_budget = max(
            0,
            self._model_window_tokens
            - task.budget.output_reserve_tokens
            - self._safety_margin_tokens,
        )
        base: dict[str, Any] = {
            "task": {
                "goal": task.goal,
                "repository_root": task.repository_root,
                "profile": task.profile.value,
                "budget_mode": task.budget.mode,
            },
            "working_state": {
                "plan": list(working_state.plan),
                "pending_items": list(working_state.pending_items),
                "evidence_ids": list(working_state.evidence_ids),
                "running_summary": working_state.running_summary,
                "human_input": working_state.human_input,
            },
            "evidence": [
                {
                    "evidence_id": item.evidence_id,
                    "source": item.source,
                    "summary": item.summary,
                    "artifact_id": item.artifact_id,
                }
                for item in evidence
            ],
            "available_tools": [
                {"name": item.name, "description": item.description}
                for item in tools
            ],
            "selected_tool_schemas": [
                {
                    "name": item.name,
                    "description": item.description,
                    "input_schema": item.input_schema,
                    "readonly": item.readonly,
                }
                for item in tools[:3]
            ],
            "recent_events": [],
        }
        selected: list[dict[str, Any]] = []
        omitted = 0
        for event in reversed(events):
            candidate = {
                "kind": event.kind,
                "payload": event.payload,
            }
            trial = {**base, "recent_events": [candidate, *selected]}
            if self._estimate(trial) <= input_budget:
                selected.insert(0, candidate)
            else:
                omitted += 1
        if omitted:
            base["history_summary"] = (
                f"{omitted} older event(s) omitted; preserve the listed task state, "
                "pending items, and Evidence references."
            )
        payload = {**base, "recent_events": selected}
        return ContextSnapshot(
            task_id=task.task_id,
            tick_sequence=tick_sequence,
            payload=payload,
            estimated_input_tokens=self._estimate(payload),
            input_budget_tokens=input_budget,
            output_reserve_tokens=task.budget.output_reserve_tokens,
            compacted=omitted > 0,
            omitted_event_count=omitted,
            tool_schemas=tuple(base["selected_tool_schemas"]),
        )

    def _estimate(self, value: dict[str, Any]) -> int:
        return max(
            1,
            self._token_meter.count(json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )
