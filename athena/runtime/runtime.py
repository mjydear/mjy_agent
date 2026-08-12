"""The single bounded-ReAct execution seam for the new Agent Runtime."""

from __future__ import annotations

import json
import hashlib
from dataclasses import replace
from typing import Any
from uuid import uuid4

from .context import RuntimeContextCompiler
from .engine import DecisionEngine, DemoDecisionEngine
from .models import (
    AdvanceResult,
    AgentTask,
    Decision,
    DecisionKind,
    Event,
    FinalReport,
    TaskStatus,
    Tick,
    TickStatus,
    ToolEffectRecord,
    Usage,
    WorkingState,
    utc_now,
)
from .store import InMemoryRuntimeStore
from .tools import ReadOnlyToolCatalog, ToolExecution

_COMPACTION_ARTIFACT_BYTES = 12_000


class AgentRuntime:
    """Advance one durable task by at most one structured logical action."""

    def __init__(
        self,
        *,
        store: InMemoryRuntimeStore,
        decision_engine: DecisionEngine | None = None,
        context_compiler: RuntimeContextCompiler | None = None,
        tools: ReadOnlyToolCatalog | None = None,
    ) -> None:
        self._store = store
        self._decision_engine = decision_engine or DemoDecisionEngine()
        self._context_compiler = context_compiler or RuntimeContextCompiler()
        self._tools = tools or ReadOnlyToolCatalog()

    def advance(
        self,
        task_id: str,
        lease_id: str,
        resume_input: str | None = None,
    ) -> AdvanceResult:
        task = self._store.claim(task_id, lease_id)
        snapshot = self._store.snapshot(task_id)
        if task.status.terminal:
            return AdvanceResult(task=snapshot.task, tick=None, decision=None, context=snapshot.context)
        if task.cancellation_requested:
            task.status = TaskStatus.CANCELLED
            task.updated_at = utc_now()
            self._store.persist_task(
                task_id=task_id,
                lease_id=lease_id,
                task=task,
                kind="task.cancelled",
                payload={},
            )
            return AdvanceResult(task=replace(task), tick=None, decision=None, context=snapshot.context)
        if task.status is TaskStatus.WAITING_HUMAN and not resume_input:
            return AdvanceResult(task=snapshot.task, tick=None, decision=None, context=snapshot.context)
        if len(snapshot.ticks) >= task.budget.max_ticks or task.budget.remaining_tokens <= 0:
            task.status = TaskStatus.BUDGET_EXHAUSTED
            task.updated_at = utc_now()
            self._store.persist_task(
                task_id=task_id,
                lease_id=lease_id,
                task=task,
                kind="task.budget_exhausted",
                payload={"budget_mode": task.budget.mode},
            )
            return AdvanceResult(task=replace(task), tick=None, decision=None, context=snapshot.context)

        working_state = snapshot.working_state
        if resume_input:
            working_state = replace(working_state, human_input=resume_input.strip())
        task.status = TaskStatus.RUNNING
        task.updated_at = utc_now()
        next_sequence = len(snapshot.ticks) + 1
        context = self._context_compiler.compile(
            task=task,
            tick_sequence=next_sequence,
            working_state=working_state,
            events=snapshot.events,
            evidence=snapshot.evidence,
            tools=self._tools.declarations,
        )
        decision = self._decision_engine.decide(context)
        tick_id = f"tick_{uuid4().hex}"
        events: list[Event] = [
            self._event(task_id, tick_id, "tick.started", {"sequence": next_sequence})
        ]
        artifacts = ()
        evidence = ()
        tick_status = TickStatus.COMPLETED

        if decision.kind is DecisionKind.TOOL_CALL:
            if not decision.tool_name or not self._tools.has(decision.tool_name):
                task.status = TaskStatus.FAILED
                tick_status = TickStatus.FAILED
                events.append(
                    self._event(
                        task_id,
                        tick_id,
                        "tool.rejected",
                        {"tool_name": decision.tool_name, "reason_code": "UNKNOWN_TOOL"},
                    )
                )
            else:
                events.append(
                    self._event(
                        task_id,
                        tick_id,
                        "tool.called",
                        {
                            "tool_name": decision.tool_name,
                            "readonly": True,
                            "effect_id": self._effect_id(
                                task_id, next_sequence, decision.tool_name, decision.arguments
                            ),
                        },
                    )
                )
                effect_id = self._effect_id(
                    task_id, next_sequence, decision.tool_name, decision.arguments
                )
                journal = getattr(self._store, "reserve_tool_effect", None)
                complete_journal = getattr(self._store, "complete_tool_effect", None)
                existing_effect = (
                    journal(
                        task_id=task_id,
                        lease_id=lease_id,
                        effect_id=effect_id,
                        tool_name=decision.tool_name,
                    )
                    if callable(journal)
                    else None
                )
                if (
                    existing_effect is not None
                    and existing_effect.status == "succeeded"
                    and existing_effect.artifact is not None
                    and existing_effect.evidence is not None
                ):
                    execution = ToolExecution(
                        artifact=existing_effect.artifact,
                        evidence=existing_effect.evidence,
                    )
                else:
                    execution = self._tools.invoke(
                        task_id=task_id,
                        tick_id=tick_id,
                        repository_root=task.repository_root,
                        tool_name=decision.tool_name,
                        arguments=decision.arguments,
                    )
                    if callable(complete_journal):
                        complete_journal(
                            task_id=task_id,
                            lease_id=lease_id,
                            effect=ToolEffectRecord(
                                effect_id=effect_id,
                                task_id=task_id,
                                tool_name=decision.tool_name,
                                status="succeeded" if execution.succeeded else "failed",
                                artifact=execution.artifact,
                                evidence=execution.evidence,
                                error_code=execution.error_code,
                                error_message=execution.error_message,
                            ),
                        )
                if not execution.succeeded:
                    task.status = TaskStatus.FAILED
                    tick_status = TickStatus.FAILED
                    events.append(
                        self._event(
                            task_id,
                            tick_id,
                            "tool.rejected",
                            {
                                "tool_name": decision.tool_name,
                                "reason_code": execution.error_code,
                            },
                        )
                    )
                else:
                    assert execution.artifact is not None and execution.evidence is not None
                    artifacts = (execution.artifact,)
                    evidence = (execution.evidence,)
                    working_state = self._next_working_state(
                        working_state, execution.artifact.content, execution.evidence.evidence_id
                    )
                    events.append(
                        self._event(
                            task_id,
                            tick_id,
                            "tool.succeeded",
                            {
                                "tool_name": decision.tool_name,
                                "artifact_id": execution.artifact.artifact_id,
                                "evidence_id": execution.evidence.evidence_id,
                            },
                        )
                    )
        elif decision.kind is DecisionKind.FINAL:
            task.status = TaskStatus.SUCCEEDED
            evidence_summaries = [item.summary for item in (*snapshot.evidence, *evidence)]
            task.final_report = FinalReport(
                root_cause=(
                    "；".join(evidence_summaries)
                    if evidence_summaries
                    else "模型已判断当前证据足以形成结论。"
                ),
                repair_recommendation=(
                    decision.response
                    or "请依据已记录的 Evidence 形成只读修复建议，并在变更前由人工审核。"
                ),
                evidence_ids=working_state.evidence_ids,
            )
            working_state = replace(working_state, pending_items=())
            events.append(
                self._event(
                    task_id,
                    tick_id,
                    "task.succeeded",
                    {"evidence_count": len(working_state.evidence_ids)},
                )
            )
        elif decision.kind is DecisionKind.ASK_HUMAN:
            task.status = TaskStatus.WAITING_HUMAN
            tick_status = TickStatus.WAITING_HUMAN
            events.append(
                self._event(
                    task_id,
                    tick_id,
                    "task.waiting_human",
                    {"question": decision.response},
                )
            )
        else:
            task.status = TaskStatus.FAILED
            tick_status = TickStatus.FAILED
            events.append(
                self._event(task_id, tick_id, "task.failed", {"message": decision.response})
            )

        events.append(
            self._event(
                task_id,
                tick_id,
                "tick.completed",
                {"decision": decision.to_public_payload(), "status": tick_status.value},
            )
        )
        tick = Tick(
            tick_id=tick_id,
            task_id=task_id,
            sequence=next_sequence,
            decision=decision,
            status=tick_status,
            created_at=utc_now(),
        )
        usage = self._usage(task, tick, context)
        task.budget = task.budget.consume(usage.actual_tokens)
        task.updated_at = utc_now()
        self._store.commit_tick(
            task_id=task_id,
            lease_id=lease_id,
            task=task,
            tick=tick,
            events=tuple(events),
            artifacts=artifacts,
            evidence=evidence,
            usage=usage,
            working_state=working_state,
            context=context,
        )
        return AdvanceResult(task=replace(task), tick=tick, decision=decision, context=context)

    @staticmethod
    def _next_working_state(
        state: WorkingState, artifact_content: dict[str, Any], evidence_id: str
    ) -> WorkingState:
        artifact_bytes = len(json.dumps(artifact_content, ensure_ascii=False).encode("utf-8"))
        compacted = artifact_bytes > _COMPACTION_ARTIFACT_BYTES
        summary = (
            "已压缩长工具输出：保留任务目标、待完成诊断与 Evidence 引用；完整结果保存在 Artifact。"
            if compacted
            else "已记录只读工具证据，下一 Tick 将继续验证根因。"
        )
        return WorkingState(
            plan=("定位实现", "复现失败", "形成修复建议"),
            pending_items=("形成最终诊断",),
            evidence_ids=(*state.evidence_ids, evidence_id),
            running_summary=summary,
            human_input=state.human_input,
            compaction_count=state.compaction_count + (1 if compacted else 0),
        )

    @staticmethod
    def _event(task_id: str, tick_id: str, kind: str, payload: dict[str, Any]) -> Event:
        return Event(
            event_id=f"event_{uuid4().hex}",
            task_id=task_id,
            tick_id=tick_id,
            sequence=0,
            kind=kind,
            payload=payload,
            created_at=utc_now(),
        )

    @staticmethod
    def _effect_id(
        task_id: str, sequence: int, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        encoded = json.dumps(
            {
                "task_id": task_id,
                "sequence": sequence,
                "tool_name": tool_name,
                "arguments": arguments,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"effect_{hashlib.sha256(encoded).hexdigest()[:48]}"

    def _usage(self, task: AgentTask, tick: Tick, context: Any) -> Usage:
        routing = getattr(self._decision_engine, "last_routing", None)
        actual_input = context.estimated_input_tokens
        actual_output = 64
        model_tier = "demo"
        route_reason = "DEMO_CODE_DIAGNOSIS"
        if routing is not None:
            actual_input = routing.actual_input_tokens or context.estimated_input_tokens
            actual_output = routing.actual_output_tokens or 64
            model_tier = routing.selected_tier
            route_reason = routing.route_reason
        reserve = actual_input + task.budget.output_reserve_tokens
        return Usage(
            usage_id=f"usage_{uuid4().hex}",
            task_id=task.task_id,
            tick_id=tick.tick_id or "",
            purpose="react_decision",
            model_tier=model_tier,
            route_reason=route_reason,
            estimated_input_tokens=actual_input,
            reserved_tokens=reserve,
            actual_input_tokens=actual_input,
            actual_output_tokens=actual_output,
            budget_mode=task.budget.mode,
            created_at=utc_now(),
        )
