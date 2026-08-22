"""Compile the Runtime four-layer memory contract for ReAct decisions."""

from __future__ import annotations

import json
from dataclasses import replace

from athena.infra.token_meter import TokenMeter
from athena.runtime.models import (
    AgentTask,
    ContextSnapshot,
    Event,
    Evidence,
    WorkingState,
)
from athena.runtime.tools import ToolDeclaration

from .layer import MemoryLayer
from .models import MemoryBudget, MemoryCheckpoint, PendingToolPair, RunningSummary


class FourLayerRuntimeContextCompiler:
    """Compile model context from durable state without including Artifact bodies."""

    def __init__(
        self,
        *,
        memory: MemoryLayer | None = None,
        model_window_tokens: int = 16_384,
        safety_margin_tokens: int = 1_024,
        token_meter: TokenMeter | None = None,
    ) -> None:
        if model_window_tokens <= 0 or safety_margin_tokens < 0:
            raise ValueError("context limits must be non-negative and non-zero")
        self._token_meter = token_meter or TokenMeter()
        self._memory = memory or MemoryLayer(token_meter=self._token_meter)
        self._model_window_tokens = model_window_tokens
        self._safety_margin_tokens = safety_margin_tokens

    def compile(
        self,
        *,
        task: AgentTask,
        tick_sequence: int,
        working_state: WorkingState,
        events: tuple[Event, ...],
        evidence: tuple[Evidence, ...],
        tools: tuple[ToolDeclaration, ...],
        tenant_id: str = "default",
    ) -> ContextSnapshot:
        checkpoint = MemoryCheckpoint(
            tick_sequence=tick_sequence,
            working_state=working_state,
            constraints=(
                "只可访问服务端给定的仓库根目录。",
                "只可使用服务端选择的只读工具。",
                "不得自动激活未经过评估和人工审核的 Skill。",
            ),
            running_summary=self._summary(working_state, events),
            unresolved_tool_pairs=self._unresolved_pairs(events),
        )
        snapshot = self._memory.compile(
            task=task,
            checkpoint=checkpoint,
            evidence=evidence,
            budget=MemoryBudget(
                model_window_tokens=self._model_window_tokens,
                output_reserve_tokens=task.budget.output_reserve_tokens,
                safety_margin_tokens=self._safety_margin_tokens,
            ),
            tenant_id=tenant_id,
        )
        tool_schemas = tuple(
            {
                "name": item.name,
                "description": item.description,
                "input_schema": item.input_schema,
                "readonly": item.readonly,
            }
            for item in tools[:3]
        )
        payload = dict(snapshot.payload)
        working_memory = dict(payload["working_memory"])
        working_memory["completed_tool_calls"] = self._completed_tool_calls(events)
        payload["working_memory"] = working_memory
        # Tool declarations are server-owned metadata, not a memory layer, but
        # they are still part of the model-visible request and must be included
        # in the input-token estimate used by budgets and Replay A/B.
        serialized = json.dumps(
            {"memory": payload, "tool_schemas": list(tool_schemas)},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        estimated_input_tokens = max(1, self._token_meter.count(serialized))
        return replace(
            snapshot,
            payload=payload,
            estimated_input_tokens=estimated_input_tokens,
            tool_schemas=tool_schemas,
        )

    @staticmethod
    def _summary(state: WorkingState, events: tuple[Event, ...]) -> RunningSummary:
        failed_attempts = tuple(
            f"{event.payload.get('tool_name', 'tool')}: "
            f"{event.payload.get('reason_code', event.kind)}"
            for event in events
            if event.kind == "tool.rejected"
        )[-4:]
        completed = (state.running_summary,) if state.running_summary else ()
        return RunningSummary(
            completed_facts=completed,
            failed_attempts=failed_attempts,
            open_questions=state.pending_items[-4:],
            next_actions=state.plan[-4:],
        )

    @staticmethod
    def _unresolved_pairs(events: tuple[Event, ...]) -> tuple[PendingToolPair, ...]:
        calls: dict[str, PendingToolPair] = {}
        completed: set[str] = set()
        for event in events:
            if event.kind == "tool.called":
                calls[event.tick_id] = PendingToolPair(
                    call_id=event.tick_id,
                    tool_name=str(event.payload.get("tool_name", "tool")),
                    request_summary="已发起受限工具调用，等待受控结果。",
                )
            elif event.kind in {"tool.succeeded", "tool.rejected"}:
                completed.add(event.tick_id)
        return tuple(
            pair for call_id, pair in calls.items() if call_id not in completed
        )

    @staticmethod
    def _completed_tool_calls(events: tuple[Event, ...]) -> list[dict[str, str]]:
        """Keep successful tool progress without replaying Artifact bodies."""

        return [
            {
                "tool_name": str(event.payload.get("tool_name", "tool")),
                "evidence_id": str(event.payload.get("evidence_id", "")),
                "status": "succeeded",
            }
            for event in events
            if event.kind == "tool.succeeded"
        ][-8:]
