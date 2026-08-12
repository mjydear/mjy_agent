"""Token-governed four-layer memory compilation for one Runtime decision."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Protocol

from athena.infra.token_meter import TokenMeter
from athena.runtime.models import AgentTask, ContextSnapshot, Evidence, WorkingState

from .models import (
    MemoryBudget,
    MemoryBudgetError,
    MemoryCheckpoint,
    RunningSummary,
)
from .retrieval import EvaluatedSkill, EvaluatedSkillRetriever, SkillRetrievalAdapter


class SummaryReducer(Protocol):
    """Replaceable reducer for a structured running summary."""

    def compact(
        self, summary: RunningSummary, *, max_items: int, text_limit: int
    ) -> RunningSummary: ...


class DeterministicSummaryReducer:
    """A local fallback that retains recent items in each structured category."""

    def compact(
        self, summary: RunningSummary, *, max_items: int, text_limit: int
    ) -> RunningSummary:
        return summary.compact(max_items=max_items, text_limit=text_limit)


class MemoryLayer:
    """Compile four memory layers without reading raw Artifact content.

    The module's interface is intentionally small: durable execution supplies a
    task and checkpoint, and the caller receives an ordinary Runtime
    ``ContextSnapshot``. Retrieval, tokenization, and reduction stay injectable
    implementation details behind this seam.
    """

    _SUMMARY_CANDIDATE_THRESHOLD = 0.75
    _FORCED_COMPACTION_THRESHOLD = 0.90
    _CONTEXT_SCHEMA_VERSION = "runtime.memory.v1"

    def __init__(
        self,
        *,
        skill_retrieval: SkillRetrievalAdapter | None = None,
        summary_reducer: SummaryReducer | None = None,
        token_meter: TokenMeter | None = None,
        skill_limit: int = 3,
    ) -> None:
        if skill_limit < 0:
            raise ValueError("skill_limit must be non-negative")
        self._skill_retriever = EvaluatedSkillRetriever(skill_retrieval)
        self._summary_reducer = summary_reducer or DeterministicSummaryReducer()
        self._token_meter = token_meter or TokenMeter()
        self._skill_limit = skill_limit

    def compile(
        self,
        task: AgentTask,
        checkpoint: MemoryCheckpoint | WorkingState,
        evidence: Iterable[Evidence],
        budget: MemoryBudget,
    ) -> ContextSnapshot:
        """Return a bounded model projection for the next decision Tick.

        At 75% of input capacity the snapshot marks a summary candidate. At
        90%, it removes optional Skills and deterministically compacts only the
        structured history; task goal, constraints, unresolved effects, and
        Evidence references remain mandatory.
        """

        normalized_checkpoint = self._normalize_checkpoint(checkpoint)
        capacity = budget.input_capacity_tokens
        if capacity <= 0:
            raise MemoryBudgetError(required_tokens=1, available_tokens=capacity)

        evidence_references = self._evidence_references(evidence)
        skills = self._skill_retriever.retrieve(
            query=task.goal,
            limit=self._skill_limit,
        )
        full_payload = self._build_payload(
            task=task,
            checkpoint=normalized_checkpoint,
            evidence=evidence_references,
            summary=normalized_checkpoint.running_summary,
            skills=skills,
            budget=budget,
            summary_candidate=False,
            forced_compaction=False,
        )
        full_tokens = self._estimate(full_payload)
        full_utilization = full_tokens / capacity
        summary_candidate = full_utilization >= self._SUMMARY_CANDIDATE_THRESHOLD
        forced_compaction = full_utilization >= self._FORCED_COMPACTION_THRESHOLD

        if forced_compaction:
            payload, estimated_tokens = self._force_compact(
                task=task,
                checkpoint=normalized_checkpoint,
                evidence=evidence_references,
                budget=budget,
                capacity=capacity,
            )
        else:
            payload = self._build_payload(
                task=task,
                checkpoint=normalized_checkpoint,
                evidence=evidence_references,
                summary=normalized_checkpoint.running_summary,
                skills=skills,
                budget=budget,
                summary_candidate=summary_candidate,
                forced_compaction=False,
            )
            estimated_tokens = self._estimate(payload)

            # The candidate marker is itself part of the model-visible
            # projection. Re-check after adding it at the 90% boundary.
            if estimated_tokens / capacity >= self._FORCED_COMPACTION_THRESHOLD:
                forced_compaction = True
                payload, estimated_tokens = self._force_compact(
                    task=task,
                    checkpoint=normalized_checkpoint,
                    evidence=evidence_references,
                    budget=budget,
                    capacity=capacity,
                )

        if estimated_tokens > capacity:
            raise MemoryBudgetError(
                required_tokens=estimated_tokens,
                available_tokens=capacity,
            )

        governance = payload["memory_governance"]
        governance["input_utilization"] = round(estimated_tokens / capacity, 4)
        governance["pre_compaction_utilization"] = round(full_utilization, 4)
        return ContextSnapshot(
            task_id=task.task_id,
            tick_sequence=normalized_checkpoint.tick_sequence,
            payload=payload,
            estimated_input_tokens=estimated_tokens,
            input_budget_tokens=capacity,
            output_reserve_tokens=budget.output_reserve_tokens,
            compacted=forced_compaction,
            omitted_event_count=0,
            compaction_count=(
                normalized_checkpoint.working_state.compaction_count
                + int(forced_compaction)
            ),
        )

    @staticmethod
    def _normalize_checkpoint(
        checkpoint: MemoryCheckpoint | WorkingState,
    ) -> MemoryCheckpoint:
        if isinstance(checkpoint, MemoryCheckpoint):
            return checkpoint
        if isinstance(checkpoint, WorkingState):
            return MemoryCheckpoint.from_working_state(checkpoint)
        raise TypeError("checkpoint must be MemoryCheckpoint or WorkingState")

    def _force_compact(
        self,
        *,
        task: AgentTask,
        checkpoint: MemoryCheckpoint,
        evidence: list[dict[str, str]],
        budget: MemoryBudget,
        capacity: int,
    ) -> tuple[dict[str, object], int]:
        max_items = 4
        text_limit = 192
        for _ in range(8):
            compact_summary = self._summary_reducer.compact(
                checkpoint.running_summary,
                max_items=max_items,
                text_limit=text_limit,
            )
            payload = self._build_payload(
                task=task,
                checkpoint=checkpoint,
                evidence=evidence,
                summary=compact_summary,
                skills=(),
                budget=budget,
                summary_candidate=True,
                forced_compaction=True,
                compact_text_limit=text_limit,
            )
            estimated_tokens = self._estimate(payload)
            if estimated_tokens <= capacity:
                return payload, estimated_tokens
            max_items = max(1, max_items // 2)
            text_limit = max(24, text_limit // 2)

        mandatory_payload = self._build_payload(
            task=task,
            checkpoint=checkpoint,
            evidence=evidence,
            summary=RunningSummary(),
            skills=(),
            budget=budget,
            summary_candidate=True,
            forced_compaction=True,
            compact_text_limit=24,
        )
        required_tokens = self._estimate(mandatory_payload)
        raise MemoryBudgetError(
            required_tokens=required_tokens,
            available_tokens=capacity,
        )

    def _build_payload(
        self,
        *,
        task: AgentTask,
        checkpoint: MemoryCheckpoint,
        evidence: list[dict[str, str]],
        summary: RunningSummary,
        skills: tuple[EvaluatedSkill, ...],
        budget: MemoryBudget,
        summary_candidate: bool,
        forced_compaction: bool,
        compact_text_limit: int | None = None,
    ) -> dict[str, object]:
        state = checkpoint.working_state
        return {
            "schema_version": self._CONTEXT_SCHEMA_VERSION,
            "task": {
                "goal": task.goal,
                "repository_root": task.repository_root,
                "profile": task.profile.value,
                "budget_mode": task.budget.mode,
                "constraints": list(checkpoint.constraints),
            },
            "working_memory": {
                "plan": list(state.plan),
                "pending_items": list(state.pending_items),
                "pinned_evidence_ids": list(state.evidence_ids),
                "human_input": state.human_input,
                "unresolved_tool_pairs": [
                    pair.to_prompt_payload(text_limit=compact_text_limit)
                    for pair in checkpoint.unresolved_tool_pairs
                ],
            },
            "running_summary": summary.to_prompt_payload(),
            # Deliberately reference-only: this layer has no Artifact resolver
            # and never receives or reads an Artifact's raw content.
            "evidence_memory": [
                self._compact_reference(item, compact_text_limit)
                for item in evidence
            ],
            "skill_memory": [skill.to_prompt_payload() for skill in skills],
            "memory_governance": {
                "input_capacity_tokens": budget.input_capacity_tokens,
                "reserved_output_tokens": budget.output_reserve_tokens,
                "safety_margin_tokens": budget.safety_margin_tokens,
                "summary_candidate": summary_candidate,
                "forced_compaction": forced_compaction,
                "artifact_content_policy": "references_only",
                "input_utilization": 0.0,
                "pre_compaction_utilization": 0.0,
            },
        }

    @staticmethod
    def _evidence_references(evidence: Iterable[Evidence]) -> list[dict[str, str]]:
        # Evidence intentionally has no Artifact-content dereference here.
        return [
            {
                "evidence_id": item.evidence_id,
                "artifact_id": item.artifact_id,
                "source": item.source,
                "summary": item.summary,
            }
            for item in evidence
        ]

    @staticmethod
    def _compact_reference(
        reference: dict[str, str], text_limit: int | None
    ) -> dict[str, str]:
        if text_limit is None or len(reference["summary"]) <= text_limit:
            return dict(reference)
        return {
            **reference,
            "summary": reference["summary"][: max(1, text_limit - 3)].rstrip()
            + "...",
        }

    def _estimate(self, payload: dict[str, object]) -> int:
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return max(1, self._token_meter.count(serialized))
