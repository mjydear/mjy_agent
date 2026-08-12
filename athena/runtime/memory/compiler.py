"""Adapter that makes the V1 memory contract usable by the ReAct runtime."""

from __future__ import annotations

from dataclasses import replace

from athena.runtime.models import AgentTask, ContextSnapshot, Event, Evidence, WorkingState
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
    ) -> None:
        if model_window_tokens <= 0 or safety_margin_tokens < 0:
            raise ValueError("context limits must be non-negative and non-zero")
        self._memory = memory or MemoryLayer()
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
        # The compatibility projection is deliberately outside MemoryLayer:
        # direct memory consumers keep the stable four-layer schema, while the
        # legacy Demo engine and Runtime Console can migrate incrementally.
        payload = dict(snapshot.payload)
        payload.update(
            {
                "working_state": {
                    "plan": list(working_state.plan),
                    "pending_items": list(working_state.pending_items),
                    "evidence_ids": list(working_state.evidence_ids),
                    "running_summary": working_state.running_summary,
                    "human_input": working_state.human_input,
                },
                "evidence": list(payload.get("evidence_memory", [])),
                "selected_tool_schemas": list(tool_schemas),
            }
        )
        return replace(snapshot, payload=payload, tool_schemas=tool_schemas)

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
        return tuple(pair for call_id, pair in calls.items() if call_id not in completed)
