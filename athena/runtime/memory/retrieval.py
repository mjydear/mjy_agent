"""Skill-memory retrieval adapters with an evaluated-only admission policy."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import SkillEvaluationState


@dataclass(frozen=True)
class EvaluatedSkill:
    """A compact Skill reference eligible for prompt projection after review."""

    skill_id: str
    title: str
    procedure_summary: str
    evaluation_state: SkillEvaluationState
    source_references: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.evaluation_state, SkillEvaluationState):
            object.__setattr__(
                self,
                "evaluation_state",
                SkillEvaluationState(self.evaluation_state),
            )
        for name, value in (
            ("skill_id", self.skill_id),
            ("title", self.title),
            ("procedure_summary", self.procedure_summary),
        ):
            if not value.strip():
                raise ValueError(f"{name} must be a non-empty string")

    @property
    def is_evaluated(self) -> bool:
        return self.evaluation_state is SkillEvaluationState.APPROVED

    def to_prompt_payload(self) -> dict[str, str | list[str]]:
        return {
            "skill_id": self.skill_id,
            "title": self.title,
            "procedure_summary": self.procedure_summary,
            "source_references": list(self.source_references),
        }


class SkillRetrievalAdapter(Protocol):
    """Retrieves candidate Skill references; filtering is enforced by the layer."""

    def retrieve(self, *, query: str, limit: int) -> Sequence[EvaluatedSkill]: ...


class InMemorySkillRetrievalAdapter:
    """Small deterministic adapter used by tests and local demonstrations."""

    def __init__(self, *, skills: Sequence[EvaluatedSkill] = ()) -> None:
        self._skills = tuple(skills)

    def retrieve(self, *, query: str, limit: int) -> tuple[EvaluatedSkill, ...]:
        if limit <= 0:
            return ()
        query_terms = frozenset(query.lower().split())

        def rank(skill: EvaluatedSkill) -> tuple[int, str]:
            text_terms = frozenset(
                f"{skill.title} {skill.procedure_summary}".lower().split()
            )
            return (-len(query_terms & text_terms), skill.skill_id)

        return tuple(sorted(self._skills, key=rank)[:limit])


class EvaluatedSkillRetriever:
    """Defensive adapter that makes unreviewed Skill output non-model-visible."""

    def __init__(self, adapter: SkillRetrievalAdapter | None = None) -> None:
        self._adapter = adapter

    def retrieve(self, *, query: str, limit: int) -> tuple[EvaluatedSkill, ...]:
        if self._adapter is None or limit <= 0:
            return ()
        try:
            candidates = self._adapter.retrieve(query=query, limit=limit)
        except Exception:
            # Retrieval is optional context. A backend outage must not create a
            # second decision failure path for the running task.
            return ()
        return tuple(skill for skill in candidates if skill.is_evaluated)[:limit]
