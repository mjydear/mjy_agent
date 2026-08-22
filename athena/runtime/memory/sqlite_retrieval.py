"""Small persistent Skill-memory adapter for offline Runtime execution."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Final

from .models import SkillEvaluationState
from .retrieval import EvaluatedSkill, SkillRetrievalAdapter

_SCHEMA: Final[str] = """
CREATE TABLE IF NOT EXISTS runtime_skill_memory (
    skill_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    procedure_summary TEXT NOT NULL,
    evaluation_state TEXT NOT NULL,
    source_references_json TEXT NOT NULL,
    updated_at INTEGER NOT NULL
)
"""


class SQLiteSkillRetrievalAdapter(SkillRetrievalAdapter):
    """Persist compact Skill references and retrieve approved matches only.

    The adapter deliberately stores the prompt projection rather than raw
    trajectories. It is synchronous so the Runtime context compiler can use it
    on the offline execution path without introducing an async seam.
    """

    def __init__(self, database_path: str | Path) -> None:
        self._database_path = str(database_path)
        if self._database_path != ":memory:":
            Path(self._database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(_SCHEMA)

    def upsert(self, skill: EvaluatedSkill) -> None:
        """Persist a Skill reference, including non-approved states."""

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_skill_memory (
                    skill_id, title, procedure_summary, evaluation_state,
                    source_references_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, strftime('%s', 'now'))
                ON CONFLICT(skill_id) DO UPDATE SET
                    title = excluded.title,
                    procedure_summary = excluded.procedure_summary,
                    evaluation_state = excluded.evaluation_state,
                    source_references_json = excluded.source_references_json,
                    updated_at = excluded.updated_at
                """,
                (
                    skill.skill_id,
                    skill.title,
                    skill.procedure_summary,
                    skill.evaluation_state.value,
                    json.dumps(list(skill.source_references), ensure_ascii=False),
                ),
            )

    def retrieve(self, *, query: str, limit: int) -> tuple[EvaluatedSkill, ...]:
        if limit <= 0:
            return ()
        terms = _terms(query)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT skill_id, title, procedure_summary, evaluation_state,
                       source_references_json
                FROM runtime_skill_memory
                WHERE evaluation_state = ?
                ORDER BY updated_at DESC, skill_id ASC
                """,
                (SkillEvaluationState.APPROVED.value,),
            ).fetchall()
        ranked: list[tuple[int, EvaluatedSkill]] = []
        for row in rows:
            skill = EvaluatedSkill(
                skill_id=row[0],
                title=row[1],
                procedure_summary=row[2],
                evaluation_state=SkillEvaluationState(row[3]),
                source_references=tuple(json.loads(row[4])),
            )
            score = len(
                terms.intersection(_terms(f"{skill.title} {skill.procedure_summary}"))
            )
            ranked.append((score, skill))
        ranked.sort(key=lambda item: (-item[0], item[1].skill_id))
        return tuple(skill for score, skill in ranked if score > 0)[:limit]

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path)
        connection.row_factory = sqlite3.Row
        return connection


def _terms(value: str) -> frozenset[str]:
    return frozenset(re.findall(r"[a-z0-9_]+", value.casefold()))


__all__ = ["SQLiteSkillRetrievalAdapter"]
