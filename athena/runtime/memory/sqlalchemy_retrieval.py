"""SQLAlchemy-backed Skill Memory for SQLite and PostgreSQL runtimes."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from athena.runtime.durable.models import RuntimeSkillMemoryModel

from .models import SkillEvaluationState
from .retrieval import EvaluatedSkill, SkillRetrievalAdapter
from .sqlite_retrieval import _terms


class SQLAlchemySkillRetrievalAdapter(SkillRetrievalAdapter):
    """Persist and retrieve compact Skill projections through the Runtime DB."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def upsert(self, skill: EvaluatedSkill) -> None:
        now = datetime.now(UTC)
        with self._sessions.begin() as session:
            row = session.get(RuntimeSkillMemoryModel, skill.skill_id)
            if row is None:
                session.add(
                    RuntimeSkillMemoryModel(
                        id=skill.skill_id,
                        title=skill.title,
                        procedure_summary=skill.procedure_summary,
                        evaluation_state=skill.evaluation_state.value,
                        source_references_json=list(skill.source_references),
                        updated_at=now,
                    )
                )
                return
            row.title = skill.title
            row.procedure_summary = skill.procedure_summary
            row.evaluation_state = skill.evaluation_state.value
            row.source_references_json = list(skill.source_references)
            row.updated_at = now

    def retrieve(self, *, query: str, limit: int) -> tuple[EvaluatedSkill, ...]:
        if limit <= 0:
            return ()
        with self._sessions() as session:
            rows = session.scalars(
                select(RuntimeSkillMemoryModel)
                .where(
                    RuntimeSkillMemoryModel.evaluation_state
                    == SkillEvaluationState.APPROVED.value
                )
                .order_by(
                    RuntimeSkillMemoryModel.updated_at.desc(),
                    RuntimeSkillMemoryModel.id.asc(),
                )
            ).all()
        query_terms = _terms(query)
        ranked: list[tuple[int, EvaluatedSkill]] = []
        for row in rows:
            skill = EvaluatedSkill(
                skill_id=row.id,
                title=row.title,
                procedure_summary=row.procedure_summary,
                evaluation_state=SkillEvaluationState(row.evaluation_state),
                source_references=tuple(row.source_references_json or ()),
            )
            score = len(
                query_terms.intersection(
                    _terms(f"{skill.title} {skill.procedure_summary}")
                )
            )
            if score > 0:
                ranked.append((score, skill))
        ranked.sort(key=lambda item: (-item[0], item[1].skill_id))
        return tuple(skill for _, skill in ranked[:limit])


__all__ = ["SQLAlchemySkillRetrievalAdapter"]
