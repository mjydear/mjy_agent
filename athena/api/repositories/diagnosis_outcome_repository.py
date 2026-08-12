"""Tenant-scoped facts for Diagnosis Outcome, Operator Feedback and Recovery."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from athena.api.repositories.models import (
    DiagnosisOutcomeModel,
    EvidenceModel,
    OperatorFeedbackModel,
    OpsTaskModel,
    RecoveryModel,
)


class DiagnosisOutcomeRepositoryError(RuntimeError):
    """Base error for fact-store invariants."""


class DiagnosticTaskNotFoundError(DiagnosisOutcomeRepositoryError):
    pass


class SupportingEvidenceNotFoundError(DiagnosisOutcomeRepositoryError):
    def __init__(self, evidence_ids: tuple[str, ...]) -> None:
        self.evidence_ids = evidence_ids
        super().__init__("supporting evidence does not belong to the Diagnostic Task")


class DiagnosisOutcomeNotFoundError(DiagnosisOutcomeRepositoryError):
    pass


class DiagnosisOutcomeConflictError(DiagnosisOutcomeRepositoryError):
    pass


class FeedbackIdempotencyConflictError(DiagnosisOutcomeRepositoryError):
    pass


@dataclass(frozen=True)
class Recovery:
    recovery_id: str
    tenant_id: str
    task_id: str
    outcome_id: str
    feedback_id: str
    observed_at: datetime
    summary: str


@dataclass(frozen=True)
class DiagnosisOutcome:
    outcome_id: str
    tenant_id: str
    task_id: str
    root_cause: str | None
    supporting_evidence_ids: tuple[str, ...]
    remediation_recommendation: str | None
    confidence: float
    evidence_sufficient: bool
    finalized_at: datetime

    @property
    def evidence_sufficiency(self) -> bool:
        """Compatibility name for callers that use the noun form."""
        return self.evidence_sufficient


@dataclass(frozen=True)
class OperatorFeedback:
    feedback_id: str
    tenant_id: str
    task_id: str
    outcome_id: str
    feedback_type: str
    corrected_root_cause: str | None
    corrected_remediation_recommendation: str | None
    note: str | None
    submitted_by: str
    idempotency_key: str
    created_at: datetime
    recovery: Recovery | None


class DiagnosisOutcomeRepository:
    """Persist immutable diagnosis facts behind a tenant-scoped interface."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def finalize(
        self,
        tenant_id: str,
        task_id: str,
        *,
        root_cause: str | None,
        supporting_evidence_ids: tuple[str, ...],
        remediation_recommendation: str | None,
        confidence: float,
        evidence_sufficient: bool,
    ) -> tuple[DiagnosisOutcome, bool]:
        """Insert one outcome, or replay the exact outcome for the task.

        The task and every supporting Evidence ID are checked in the same
        transaction as the immutable insert.  The unique task constraint is
        the final concurrency guard; a competing identical insert is replayed
        after the transaction rolls back, while a different insert is a
        conflict.
        """
        evidence_ids = tuple(supporting_evidence_ids)
        outcome_hash = _outcome_hash(
            root_cause=root_cause,
            supporting_evidence_ids=evidence_ids,
            remediation_recommendation=remediation_recommendation,
            confidence=confidence,
            evidence_sufficient=evidence_sufficient,
        )
        now = datetime.now(UTC)
        outcome_id = f"outcome-{uuid.uuid4().hex}"
        values: dict[str, object] = {
            "id": outcome_id,
            "tenant_id": tenant_id,
            "task_id": task_id,
            "root_cause": root_cause,
            "supporting_evidence_ids_json": list(evidence_ids),
            "remediation_recommendation": remediation_recommendation,
            "confidence": float(confidence),
            "evidence_sufficient": evidence_sufficient,
            "outcome_hash": outcome_hash,
            "finalized_at": now,
            "created_at": now,
        }
        async with self._sessions() as session:
            async with session.begin():
                await self._assert_task_and_evidence(
                    session, tenant_id, task_id, evidence_ids
                )
                existing = await session.scalar(
                    select(DiagnosisOutcomeModel)
                    .where(
                        DiagnosisOutcomeModel.tenant_id == tenant_id,
                        DiagnosisOutcomeModel.task_id == task_id,
                    )
                    .with_for_update()
                )
                if existing is not None:
                    if existing.outcome_hash != outcome_hash:
                        raise DiagnosisOutcomeConflictError(
                            "a Diagnostic Task already has a different outcome"
                        )
                    return _outcome_from_model(existing), True

                await self._insert_outcome_if_absent(session, values)
                stored = await session.scalar(
                    select(DiagnosisOutcomeModel).where(
                        DiagnosisOutcomeModel.tenant_id == tenant_id,
                        DiagnosisOutcomeModel.task_id == task_id,
                    )
                )
                if stored is None:
                    raise RuntimeError("outcome insert did not produce a durable fact")
                if stored.outcome_hash != outcome_hash:
                    raise DiagnosisOutcomeConflictError(
                        "a Diagnostic Task already has a different outcome"
                    )
                return _outcome_from_model(stored), stored.id != outcome_id

    async def get(
        self, tenant_id: str, outcome_id: str
    ) -> DiagnosisOutcome | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(DiagnosisOutcomeModel).where(
                    DiagnosisOutcomeModel.tenant_id == tenant_id,
                    DiagnosisOutcomeModel.id == outcome_id,
                )
            )
        return _outcome_from_model(model) if model is not None else None

    async def get_for_task(
        self, tenant_id: str, task_id: str
    ) -> DiagnosisOutcome | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(DiagnosisOutcomeModel).where(
                    DiagnosisOutcomeModel.tenant_id == tenant_id,
                    DiagnosisOutcomeModel.task_id == task_id,
                )
            )
        return _outcome_from_model(model) if model is not None else None

    async def get_feedback(
        self, tenant_id: str, task_id: str, outcome_id: str
    ) -> tuple[OperatorFeedback, ...]:
        """Return feedback only when all tenant, task and outcome IDs match.

        Returning an empty sequence for a non-matching scope avoids exposing
        whether a feedback fact exists in another tenant or Diagnostic Task.
        """
        async with self._sessions() as session:
            models = (
                await session.scalars(
                    select(OperatorFeedbackModel)
                    .where(
                        OperatorFeedbackModel.tenant_id == tenant_id,
                        OperatorFeedbackModel.task_id == task_id,
                        OperatorFeedbackModel.outcome_id == outcome_id,
                    )
                    .order_by(
                        OperatorFeedbackModel.created_at,
                        OperatorFeedbackModel.id,
                    )
                )
            ).all()
            if not models:
                return ()
            feedback_ids = tuple(model.id for model in models)
            recovery_models = (
                await session.scalars(
                    select(RecoveryModel).where(
                        RecoveryModel.tenant_id == tenant_id,
                        RecoveryModel.task_id == task_id,
                        RecoveryModel.outcome_id == outcome_id,
                        RecoveryModel.feedback_id.in_(feedback_ids),
                    )
                )
            ).all()
        recovery_by_feedback = {
            model.feedback_id: _recovery_from_model(model)
            for model in recovery_models
        }
        return tuple(
            _feedback_from_model(model, recovery_by_feedback.get(model.id))
            for model in models
        )

    async def record_feedback(
        self,
        tenant_id: str,
        task_id: str,
        outcome_id: str,
        *,
        feedback_type: str,
        corrected_root_cause: str | None,
        corrected_remediation_recommendation: str | None,
        note: str | None,
        submitted_by: str,
        idempotency_key: str,
        request_hash: str,
        recovery_observed_at: datetime | None,
        recovery_summary: str | None,
    ) -> tuple[OperatorFeedback, bool]:
        """Record feedback atomically with its optional Recovery observation."""
        now = datetime.now(UTC)
        try:
            async with self._sessions() as session:
                async with session.begin():
                    await self._assert_outcome_ownership(
                        session, tenant_id, task_id, outcome_id
                    )
                    existing = await session.scalar(
                        select(OperatorFeedbackModel)
                        .where(
                            OperatorFeedbackModel.tenant_id == tenant_id,
                            OperatorFeedbackModel.idempotency_key == idempotency_key,
                        )
                        .with_for_update()
                    )
                    if existing is not None:
                        if existing.request_hash != request_hash:
                            raise FeedbackIdempotencyConflictError(
                                "idempotency key was used for different feedback"
                            )
                        recovery = await self._recovery_for_feedback(
                            session, tenant_id, existing.id
                        )
                        return _feedback_from_model(existing, recovery), True

                    model = OperatorFeedbackModel(
                        id=f"feedback-{uuid.uuid4().hex}",
                        tenant_id=tenant_id,
                        task_id=task_id,
                        outcome_id=outcome_id,
                        feedback_type=feedback_type,
                        corrected_root_cause=corrected_root_cause,
                        corrected_remediation_recommendation=(
                            corrected_remediation_recommendation
                        ),
                        note=note,
                        submitted_by=submitted_by,
                        idempotency_key=idempotency_key,
                        request_hash=request_hash,
                        created_at=now,
                    )
                    session.add(model)
                    await session.flush()
                    recovery = None
                    if recovery_observed_at is not None:
                        if recovery_summary is None:
                            raise ValueError(
                                "recovery summary is required with an observation"
                            )
                        recovery_model = RecoveryModel(
                            id=f"recovery-{uuid.uuid4().hex}",
                            tenant_id=tenant_id,
                            task_id=task_id,
                            outcome_id=outcome_id,
                            feedback_id=model.id,
                            observed_at=recovery_observed_at,
                            summary=recovery_summary,
                            created_at=now,
                        )
                        session.add(recovery_model)
                        await session.flush()
                        recovery = _recovery_from_model(recovery_model)
                    return _feedback_from_model(model, recovery), False
        except IntegrityError:
            existing = await self._get_feedback_by_key(tenant_id, idempotency_key)
            if existing is None:
                raise
            if existing.request_hash != request_hash:
                raise FeedbackIdempotencyConflictError(
                    "idempotency key was used for different feedback"
                ) from None
            return existing, True

    async def _get_feedback_by_key(
        self, tenant_id: str, idempotency_key: str
    ) -> OperatorFeedback | None:
        async with self._sessions() as session:
            model = await session.scalar(
                select(OperatorFeedbackModel).where(
                    OperatorFeedbackModel.tenant_id == tenant_id,
                    OperatorFeedbackModel.idempotency_key == idempotency_key,
                )
            )
            if model is None:
                return None
            recovery = await self._recovery_for_feedback(session, tenant_id, model.id)
        return _feedback_from_model(model, recovery)

    @staticmethod
    async def _insert_outcome_if_absent(
        session: AsyncSession, values: dict[str, object]
    ) -> None:
        """Use the database's conflict primitive instead of exception replay.

        SQLite's in-memory StaticPool can share one physical connection across
        concurrent AsyncSessions.  A duplicate `flush()` then rolls back more
        than the losing session.  `ON CONFLICT DO NOTHING` keeps both SQLite
        and PostgreSQL transactions valid, after which the caller reads the
        sole stored fact and compares its hash.
        """
        dialect_name = session.get_bind().dialect.name
        if dialect_name == "sqlite":
            statement = sqlite_insert(DiagnosisOutcomeModel).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=("tenant_id", "task_id")
            )
            await session.execute(statement)
            return
        if dialect_name == "postgresql":
            statement = postgresql_insert(DiagnosisOutcomeModel).values(**values)
            statement = statement.on_conflict_do_nothing(
                index_elements=("tenant_id", "task_id")
            )
            await session.execute(statement)
            return
        session.add(DiagnosisOutcomeModel(**values))
        await session.flush()

    @staticmethod
    async def _assert_task_and_evidence(
        session: AsyncSession,
        tenant_id: str,
        task_id: str,
        evidence_ids: tuple[str, ...],
    ) -> None:
        task = await session.scalar(
            select(OpsTaskModel)
            .where(OpsTaskModel.tenant_id == tenant_id, OpsTaskModel.id == task_id)
            .with_for_update()
        )
        if task is None:
            raise DiagnosticTaskNotFoundError(
                "Diagnostic Task does not exist for this tenant"
            )
        if not evidence_ids:
            return
        rows = (
            await session.scalars(
                select(EvidenceModel.id).where(
                    EvidenceModel.tenant_id == tenant_id,
                    EvidenceModel.task_id == task_id,
                    EvidenceModel.id.in_(evidence_ids),
                )
            )
        ).all()
        found = set(rows)
        missing = tuple(evidence_id for evidence_id in evidence_ids if evidence_id not in found)
        if missing:
            raise SupportingEvidenceNotFoundError(missing)

    @staticmethod
    async def _assert_outcome_ownership(
        session: AsyncSession, tenant_id: str, task_id: str, outcome_id: str
    ) -> None:
        task = await session.scalar(
            select(OpsTaskModel).where(
                OpsTaskModel.tenant_id == tenant_id,
                OpsTaskModel.id == task_id,
            )
        )
        outcome = await session.scalar(
            select(DiagnosisOutcomeModel)
            .where(
                DiagnosisOutcomeModel.tenant_id == tenant_id,
                DiagnosisOutcomeModel.task_id == task_id,
                DiagnosisOutcomeModel.id == outcome_id,
            )
            .with_for_update()
        )
        if task is None or outcome is None:
            raise DiagnosisOutcomeNotFoundError(
                "Diagnosis Outcome does not belong to this tenant and task"
            )

    @staticmethod
    async def _recovery_for_feedback(
        session: AsyncSession, tenant_id: str, feedback_id: str
    ) -> Recovery | None:
        model = await session.scalar(
            select(RecoveryModel).where(
                RecoveryModel.tenant_id == tenant_id,
                RecoveryModel.feedback_id == feedback_id,
            )
        )
        return _recovery_from_model(model) if model is not None else None


def _outcome_hash(
    *,
    root_cause: str | None,
    supporting_evidence_ids: tuple[str, ...],
    remediation_recommendation: str | None,
    confidence: float,
    evidence_sufficient: bool,
) -> str:
    payload = {
        "confidence": float(confidence),
        "evidence_sufficient": bool(evidence_sufficient),
        "remediation_recommendation": remediation_recommendation,
        "root_cause": root_cause,
        "supporting_evidence_ids": sorted(set(supporting_evidence_ids)),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _outcome_from_model(model: DiagnosisOutcomeModel) -> DiagnosisOutcome:
    return DiagnosisOutcome(
        outcome_id=model.id,
        tenant_id=model.tenant_id,
        task_id=model.task_id,
        root_cause=model.root_cause,
        supporting_evidence_ids=tuple(model.supporting_evidence_ids_json or ()),
        remediation_recommendation=model.remediation_recommendation,
        confidence=float(model.confidence),
        evidence_sufficient=bool(model.evidence_sufficient),
        finalized_at=_as_utc(model.finalized_at),
    )


def _feedback_from_model(
    model: OperatorFeedbackModel, recovery: Recovery | None
) -> OperatorFeedback:
    return OperatorFeedback(
        feedback_id=model.id,
        tenant_id=model.tenant_id,
        task_id=model.task_id,
        outcome_id=model.outcome_id,
        feedback_type=model.feedback_type,
        corrected_root_cause=model.corrected_root_cause,
        corrected_remediation_recommendation=(
            model.corrected_remediation_recommendation
        ),
        note=model.note,
        submitted_by=model.submitted_by,
        idempotency_key=model.idempotency_key,
        created_at=_as_utc(model.created_at),
        recovery=recovery,
    )


def _recovery_from_model(model: RecoveryModel) -> Recovery:
    return Recovery(
        recovery_id=model.id,
        tenant_id=model.tenant_id,
        task_id=model.task_id,
        outcome_id=model.outcome_id,
        feedback_id=model.feedback_id,
        observed_at=_as_utc(model.observed_at),
        summary=model.summary,
    )


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
