"""Persistent and offline adapters for Episodic and Semantic Memory."""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from athena.runtime.durable.models import (
    RuntimeEpisodicMemoryModel,
    RuntimeSemanticMemoryModel,
)
from athena.runtime.learning.trajectory import TrajectoryStatus, TrajectorySummary

from .models import EpisodicMemory, SemanticMemory, SemanticMemoryState


def _terms(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9_\u4e00-\u9fff]+", value.casefold()))


class EpisodicMemoryAdapter:
    def upsert(self, memory: EpisodicMemory) -> None: ...

    def retrieve(
        self, *, query: str, tenant_id: str, limit: int
    ) -> Sequence[EpisodicMemory]: ...


class SemanticMemoryAdapter:
    def submit(self, memory: SemanticMemory) -> None: ...

    def approve(self, memory_id: str, *, tenant_id: str, reviewed_by: str) -> None: ...

    def retrieve(
        self, *, query: str, tenant_id: str, limit: int
    ) -> Sequence[SemanticMemory]: ...


class InMemoryEpisodicMemoryAdapter:
    def __init__(self, *, memories: Sequence[EpisodicMemory] = ()) -> None:
        self._memories = {item.memory_id: item for item in memories}

    def upsert(self, memory: EpisodicMemory) -> None:
        self._memories[memory.memory_id] = memory

    def retrieve(
        self, *, query: str, tenant_id: str, limit: int
    ) -> tuple[EpisodicMemory, ...]:
        return _rank(
            (item for item in self._memories.values() if item.tenant_id == tenant_id),
            query=query,
            text=lambda item: f"{item.task_summary} {item.outcome_summary}",
            limit=limit,
        )


class InMemorySemanticMemoryAdapter:
    def __init__(self, *, memories: Sequence[SemanticMemory] = ()) -> None:
        self._memories = {item.memory_id: item for item in memories}

    def submit(self, memory: SemanticMemory) -> None:
        self._memories[memory.memory_id] = memory

    def approve(self, memory_id: str, *, tenant_id: str, reviewed_by: str) -> None:
        memory = self._memories[memory_id]
        if memory.tenant_id != tenant_id:
            raise KeyError(memory_id)
        self._memories[memory_id] = SemanticMemory(
            **{
                **memory.__dict__,
                "state": SemanticMemoryState.APPROVED,
                "reviewed_by": reviewed_by,
            }
        )

    def retrieve(
        self, *, query: str, tenant_id: str, limit: int
    ) -> tuple[SemanticMemory, ...]:
        return _rank(
            (
                item
                for item in self._memories.values()
                if item.tenant_id == tenant_id
                and item.state is SemanticMemoryState.APPROVED
            ),
            query=query,
            text=lambda item: f"{item.domain} {item.fact}",
            limit=limit,
        )


class SQLAlchemyEpisodicMemoryAdapter:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def upsert(self, memory: EpisodicMemory) -> None:
        with self._sessions.begin() as session:
            row = session.get(RuntimeEpisodicMemoryModel, memory.memory_id)
            values = {
                "tenant_id": memory.tenant_id,
                "source_task_id": memory.source_task_id,
                "task_summary": memory.task_summary,
                "outcome_summary": memory.outcome_summary,
                "tool_names_json": list(memory.tool_names),
                "evidence_summaries_json": list(memory.evidence_summaries),
                "quality_score": memory.quality_score,
                "created_at": memory.created_at,
            }
            if row is None:
                session.add(RuntimeEpisodicMemoryModel(id=memory.memory_id, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    def retrieve(
        self, *, query: str, tenant_id: str, limit: int
    ) -> tuple[EpisodicMemory, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(RuntimeEpisodicMemoryModel)
                .where(RuntimeEpisodicMemoryModel.tenant_id == tenant_id)
                .order_by(RuntimeEpisodicMemoryModel.quality_score.desc())
            ).all()
        return _rank_models(rows, query=query, limit=limit, convert=_episodic_from_row)


class SQLAlchemySemanticMemoryAdapter:
    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def submit(self, memory: SemanticMemory) -> None:
        with self._sessions.begin() as session:
            row = session.get(RuntimeSemanticMemoryModel, memory.memory_id)
            values = {
                "tenant_id": memory.tenant_id,
                "domain": memory.domain,
                "fact": memory.fact,
                "confidence": memory.confidence,
                "source_trajectory_ids_json": list(memory.source_trajectory_ids),
                "state": memory.state.value,
                "reviewed_by": memory.reviewed_by,
                "created_at": memory.created_at,
                "updated_at": datetime.now(UTC),
            }
            if row is None:
                session.add(RuntimeSemanticMemoryModel(id=memory.memory_id, **values))
            else:
                for key, value in values.items():
                    setattr(row, key, value)

    def approve(self, memory_id: str, *, tenant_id: str, reviewed_by: str) -> None:
        with self._sessions.begin() as session:
            row = session.get(RuntimeSemanticMemoryModel, memory_id)
            if row is None or row.tenant_id != tenant_id:
                raise KeyError(memory_id)
            row.state = SemanticMemoryState.APPROVED.value
            row.reviewed_by = reviewed_by
            row.updated_at = datetime.now(UTC)

    def retrieve(
        self, *, query: str, tenant_id: str, limit: int
    ) -> tuple[SemanticMemory, ...]:
        with self._sessions() as session:
            rows = session.scalars(
                select(RuntimeSemanticMemoryModel)
                .where(
                    RuntimeSemanticMemoryModel.tenant_id == tenant_id,
                    RuntimeSemanticMemoryModel.state
                    == SemanticMemoryState.APPROVED.value,
                )
                .order_by(RuntimeSemanticMemoryModel.confidence.desc())
            ).all()
        return _rank_models(rows, query=query, limit=limit, convert=_semantic_from_row)


class EpisodicMemoryProjector:
    """Convert only an eligible redacted trajectory into Episodic Memory."""

    def from_trajectory(self, trajectory: TrajectorySummary) -> EpisodicMemory | None:
        if trajectory.status is not TrajectoryStatus.ELIGIBLE:
            return None
        return EpisodicMemory(
            memory_id=f"episodic-{trajectory.trajectory_id}",
            tenant_id=trajectory.tenant_id,
            source_task_id=trajectory.source_task_id,
            task_summary=trajectory.task_summary,
            outcome_summary="; ".join(
                f"{key}: {value}" for key, value in trajectory.outcome_summary.items()
            ),
            tool_names=tuple(
                str(item.get("tool_name") or "")
                for item in trajectory.tool_calls
                if str(item.get("tool_name") or "")
            ),
            evidence_summaries=tuple(
                str(item.get("summary") or "")
                for item in trajectory.evidence
                if str(item.get("summary") or "")
            ),
            quality_score=trajectory.admission.quality_score,
            created_at=trajectory.created_at,
        )


def _rank(
    items: Sequence[object], *, query: str, text, limit: int
) -> tuple[object, ...]:
    if limit <= 0:
        return ()
    query_terms = _terms(query)
    ranked = [
        (len(query_terms.intersection(_terms(text(item)))), item) for item in items
    ]
    ranked = [item for item in ranked if item[0] > 0]
    ranked.sort(key=lambda item: (-item[0], getattr(item[1], "memory_id", "")))
    return tuple(item for _, item in ranked[:limit])


def _rank_models(rows, *, query: str, limit: int, convert):
    items = tuple(convert(row) for row in rows)
    return _rank(items, query=query, text=lambda item: _memory_text(item), limit=limit)


def _memory_text(item: EpisodicMemory | SemanticMemory) -> str:
    if isinstance(item, EpisodicMemory):
        return f"{item.task_summary} {item.outcome_summary} {' '.join(item.tool_names)}"
    return f"{item.domain} {item.fact}"


def _episodic_from_row(row: RuntimeEpisodicMemoryModel) -> EpisodicMemory:
    return EpisodicMemory(
        memory_id=row.id,
        tenant_id=row.tenant_id,
        source_task_id=row.source_task_id,
        task_summary=row.task_summary,
        outcome_summary=row.outcome_summary,
        tool_names=tuple(row.tool_names_json or ()),
        evidence_summaries=tuple(row.evidence_summaries_json or ()),
        quality_score=row.quality_score,
        created_at=row.created_at,
    )


def _semantic_from_row(row: RuntimeSemanticMemoryModel) -> SemanticMemory:
    return SemanticMemory(
        memory_id=row.id,
        tenant_id=row.tenant_id,
        domain=row.domain,
        fact=row.fact,
        confidence=row.confidence,
        source_trajectory_ids=tuple(row.source_trajectory_ids_json or ()),
        state=SemanticMemoryState(row.state),
        reviewed_by=row.reviewed_by,
        created_at=row.created_at,
    )


__all__ = [
    "EpisodicMemoryAdapter",
    "InMemoryEpisodicMemoryAdapter",
    "InMemorySemanticMemoryAdapter",
    "EpisodicMemoryProjector",
    "SQLAlchemyEpisodicMemoryAdapter",
    "SQLAlchemySemanticMemoryAdapter",
    "SemanticMemoryAdapter",
]
