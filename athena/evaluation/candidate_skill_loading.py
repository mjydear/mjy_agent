"""Progressive, one-shot Candidate Skill loading for offline Runtime evaluation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any

from athena.infra.token_meter import TokenMeter
from athena.learning.skill_candidate import SkillCandidate
from athena.runtime.memory import FourLayerRuntimeContextCompiler
from athena.runtime.models import ContextSnapshot

_REFERENCE_INTENT_TERMS = frozenset(
    {
        "error",
        "fail",
        "failure",
        "forbidden",
        "human",
        "inject",
        "missing",
        "policy",
        "recover",
        "reject",
        "security",
        "unsafe",
    }
)


def candidate_trigger_matches(candidate: SkillCandidate, goal: str) -> bool:
    """Return a deterministic high-relevance match for Candidate trigger keywords."""

    trigger = candidate.trigger or {}
    keywords = trigger.get("keywords", [])
    if not isinstance(keywords, (list, tuple)) or not keywords:
        return False
    normalized_goal = f" {_normalized_text(goal)} "
    return any(
        normalized and f" {normalized} " in normalized_goal
        for item in keywords
        if (normalized := _normalized_text(str(item)))
    )


def candidate_reference_needed(candidate: SkillCandidate, goal: str) -> bool:
    """Load ancillary contracts only for a matched recovery/safety-oriented task."""

    if not candidate_trigger_matches(candidate, goal):
        return False
    terms = set(re.findall(r"[a-z0-9_]+", goal.casefold()))
    return bool(terms & _REFERENCE_INTENT_TERMS)


def candidate_index_name(candidate: SkillCandidate) -> str:
    """Return the bounded Skill identifier projected into model context."""

    return _bounded_text(candidate.name, 24)


@dataclass
class CandidateSkillLoadAudit:
    """Runtime-observed progressive loading facts for one task execution."""

    injection_count: int = 0
    index_load_count: int = 0
    procedure_load_count: int = 0
    reference_load_count: int = 0
    repeat_injection_avoided_count: int = 0
    trigger_matched: bool = False
    reference_needed: bool = False
    loaded_layers: list[str] = field(default_factory=list)
    injected_tick_sequences: list[int] = field(default_factory=list)
    layer_input_tokens: dict[str, int] = field(default_factory=dict)
    tool_context_token_delta: int = 0
    duplicate_text_omissions: int = 0

    def to_dict(self) -> dict[str, object]:
        return {
            "injection_count": self.injection_count,
            "index_load_count": self.index_load_count,
            "procedure_load_count": self.procedure_load_count,
            "reference_load_count": self.reference_load_count,
            "repeat_injection_avoided_count": self.repeat_injection_avoided_count,
            "trigger_matched": self.trigger_matched,
            "reference_needed": self.reference_needed,
            "loaded_layers": list(self.loaded_layers),
            "injected_tick_sequences": list(self.injected_tick_sequences),
            "layer_input_tokens": dict(self.layer_input_tokens),
            "tool_context_token_delta": self.tool_context_token_delta,
            "duplicate_text_omissions": self.duplicate_text_omissions,
        }


class CandidateSkillContextCompiler:
    """Inject an Index once, then add Procedure/Reference only when justified."""

    def __init__(self, candidate: SkillCandidate) -> None:
        self._candidate = candidate
        # Baseline and Candidate must compile the same four-layer Runtime
        # memory projection. Candidate-only fields are added after that shared
        # projection so Replay A/B measures the Skill cost, not two compilers.
        self._base = FourLayerRuntimeContextCompiler()
        self._meter = TokenMeter()
        self._injected_task_ids: set[str] = set()
        self.audit = CandidateSkillLoadAudit()

    def compile(self, **kwargs: Any) -> ContextSnapshot:
        all_tools = tuple(kwargs["tools"])
        tools = tuple(
            tool for tool in all_tools if tool.name in self._candidate.allowed_tools
        )
        unfiltered = self._base.compile(**{**kwargs, "tools": all_tools})
        snapshot = self._base.compile(**{**kwargs, "tools": tools})
        base_payload_tokens = self._count(snapshot.payload)
        fixed_tool_tokens = snapshot.estimated_input_tokens - base_payload_tokens
        self.audit.tool_context_token_delta += (
            snapshot.estimated_input_tokens - unfiltered.estimated_input_tokens
        )
        task = kwargs["task"]
        task_id = str(getattr(task, "task_id", "")) or "__unknown_task__"
        if task_id in self._injected_task_ids:
            self.audit.repeat_injection_avoided_count += 1
            return snapshot

        working_state = kwargs["working_state"]
        evidence = tuple(kwargs["evidence"])
        self.audit.trigger_matched = candidate_trigger_matches(
            self._candidate, task.goal
        )
        self.audit.reference_needed = candidate_reference_needed(
            self._candidate, task.goal
        )

        layers: list[tuple[str, dict[str, object]]] = [
            (
                "skill_index",
                {
                    "name": candidate_index_name(self._candidate),
                    # The index is a routing hint, not the full Skill body.
                    # Keep the durable Candidate as the source of truth and
                    # load the longer procedure only after a trigger match.
                    "description": _bounded_text(self._candidate.description, 40),
                    "trigger": dict(self._candidate.trigger or {}),
                    "risk_level": self._candidate.risk_level,
                },
            )
        ]
        if self.audit.trigger_matched:
            procedure_steps, omitted = _deduplicated_strings(
                self._candidate.procedure.get("steps", []),
                task_goal=task.goal,
                working_state=working_state,
                evidence=evidence,
            )
            procedure_steps = [_bounded_text(step, 24) for step in procedure_steps]
            self.audit.duplicate_text_omissions += omitted
            layers.append(("skill_procedure", {"steps": procedure_steps}))
        if self.audit.reference_needed:
            failure_recovery, failure_omitted = _deduplicated_strings(
                self._candidate.failure_recovery,
                task_goal=task.goal,
                working_state=working_state,
                evidence=evidence,
            )
            evidence_requirements, evidence_omitted = _deduplicated_strings(
                self._candidate.evidence_requirements,
                task_goal=task.goal,
                working_state=working_state,
                evidence=evidence,
            )
            self.audit.duplicate_text_omissions += failure_omitted + evidence_omitted
            layers.append(
                (
                    "skill_reference",
                    {
                        "failure_recovery": failure_recovery,
                        "success_contract": dict(
                            self._candidate.success_contract or {}
                        ),
                        "evidence_requirements": evidence_requirements,
                    },
                )
            )

        payload = dict(snapshot.payload)
        previous_tokens = snapshot.estimated_input_tokens
        for layer_name, layer in layers:
            payload[layer_name] = layer
            current_tokens = self._count(payload) + fixed_tool_tokens
            self.audit.layer_input_tokens[layer_name] = current_tokens - previous_tokens
            previous_tokens = current_tokens
            self.audit.loaded_layers.append(layer_name)

        self._injected_task_ids.add(task_id)
        self.audit.injection_count += 1
        self.audit.index_load_count += 1
        self.audit.procedure_load_count += int(self.audit.trigger_matched)
        self.audit.reference_load_count += int(self.audit.reference_needed)
        self.audit.injected_tick_sequences.append(int(kwargs["tick_sequence"]))
        return replace(
            snapshot,
            payload=payload,
            estimated_input_tokens=previous_tokens,
        )

    def _count(self, payload: dict[str, Any]) -> int:
        # Keep the Candidate estimate byte-for-byte aligned with the final
        # FourLayerRuntimeContextCompiler projection.
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return max(1, self._meter.count(serialized))


def _deduplicated_strings(
    values: object,
    *,
    task_goal: str,
    working_state: Any,
    evidence: tuple[Any, ...],
) -> tuple[list[str], int]:
    if not isinstance(values, (list, tuple)):
        return [], 0
    existing = {
        _dedupe_key(task_goal),
        _dedupe_key(str(working_state.running_summary or "")),
        *(_dedupe_key(str(item)) for item in working_state.plan),
        *(_dedupe_key(str(item)) for item in working_state.pending_items),
        *(_dedupe_key(str(item.summary)) for item in evidence),
    }
    retained: list[str] = []
    omitted = 0
    seen: set[str] = set()
    for value in values:
        text = str(value).strip()
        key = _dedupe_key(text)
        if not key or key in existing or key in seen:
            omitted += 1
            continue
        retained.append(text)
        seen.add(key)
    return retained, omitted


def _dedupe_key(value: str) -> str:
    return value.strip()


def _normalized_text(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9_]+", value.casefold()))


def _bounded_text(value: str, limit: int) -> str:
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 3)].rstrip() + "..."


__all__ = [
    "CandidateSkillContextCompiler",
    "CandidateSkillLoadAudit",
    "candidate_index_name",
    "candidate_reference_needed",
    "candidate_trigger_matches",
]
