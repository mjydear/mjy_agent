from __future__ import annotations

from datetime import UTC, datetime

import pytest

from athena.api.repositories import (
    Database,
    DiagnosisOutcomeRepository,
    EvidenceRepository,
    TaskCreate,
    TaskRepository,
)
from athena.application.diagnosis_outcome_service import DiagnosisOutcomeService
from athena.application.operator_feedback_service import (
    OperatorFeedbackService,
    RecoveryObservation,
)
from athena.application.verified_learning_source_resolver import (
    DurableVerifiedLearningSourceResolver,
)
from athena.config import DatabaseSettings
from athena.infra.evidence_content import LocalEvidenceContentStore


async def _setup(tmp_path):
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    await tasks.create_task(
        TaskCreate(
            task_id="task-learning-1",
            tenant_id="tenant-a",
            objective="diagnose image pull failure",
            environment_id="default",
            environment_mode="mock",
            scope={"namespace": "payment"},
            policy_snapshot={"readonly": True},
            config_snapshot={},
            budget={"remaining_steps": 4, "remaining_tokens": 6000},
            execution_profile="bounded_policy_loop",
            workflow_type="image_pull",
        ),
        idempotency_key="task-learning-1",
        request_hash="task-learning-1",
    )
    evidence = EvidenceRepository(
        database.session_factory,
        LocalEvidenceContentStore(tmp_path / "evidence", max_content_bytes=100_000),
    )
    item = await evidence.create(
        tenant_id="tenant-a",
        task_id="task-learning-1",
        evidence_type="resource_snapshot",
        source="k8s.image_pull.diagnose",
        data_origin="mock",
        summary="ImagePullBackOff event was observed",
        content={"event": "ImagePullBackOff"},
    )
    outcome_repository = DiagnosisOutcomeRepository(database.session_factory)
    outcomes = DiagnosisOutcomeService(outcome_repository)
    feedback = OperatorFeedbackService(outcome_repository)
    return database, evidence, outcomes, feedback, outcome_repository, item


@pytest.mark.asyncio
async def test_resolver_returns_only_verified_metadata(tmp_path) -> None:
    database, evidence, outcomes, feedback, outcome_repository, item = await _setup(
        tmp_path
    )
    try:
        outcome = await outcomes.finalize(
            "tenant-a",
            "task-learning-1",
            root_cause="registry endpoint rejected the image request",
            supporting_evidence_ids=(item.evidence_id,),
            remediation_recommendation="verify imagePullSecret and registry reachability",
            confidence=0.9,
            evidence_sufficient=True,
        )
        operator_feedback = await feedback.record(
            "tenant-a",
            "task-learning-1",
            outcome.outcome_id,
            feedback_type="confirmed",
            idempotency_key="feedback-learning-1",
            recovery=RecoveryObservation(
                observed_at=datetime.now(UTC),
                summary="Pod became Ready after the approved change",
            ),
        )
        resolver = DurableVerifiedLearningSourceResolver(
            outcome_repository, evidence
        )

        source = await resolver.resolve(
            "tenant-a",
            outcome_id=outcome.outcome_id,
            feedback_id=operator_feedback.feedback_id,
            evidence_ids=(item.evidence_id,),
        )

        assert source is not None
        assert source.outcome_verified is True
        assert source.feedback_verified is True
        assert source.evidence_ids == (item.evidence_id,)
        assert source.evidence[0].summary == item.summary
        assert not hasattr(source.evidence[0], "content")
        assert "thought" not in str(source).lower()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_resolver_rejects_feedback_without_recovery_or_wrong_evidence(tmp_path) -> None:
    database, evidence, outcomes, feedback, outcome_repository, item = await _setup(
        tmp_path
    )
    try:
        outcome = await outcomes.finalize(
            "tenant-a",
            "task-learning-1",
            root_cause="supported root",
            supporting_evidence_ids=(item.evidence_id,),
            remediation_recommendation="observe",
            confidence=0.8,
            evidence_sufficient=True,
        )
        operator_feedback = await feedback.record(
            "tenant-a",
            "task-learning-1",
            outcome.outcome_id,
            feedback_type="confirmed",
            idempotency_key="feedback-learning-2",
        )
        resolver = DurableVerifiedLearningSourceResolver(
            outcome_repository, evidence
        )

        assert (
            await resolver.resolve(
                "tenant-a",
                outcome_id=outcome.outcome_id,
                feedback_id=operator_feedback.feedback_id,
                evidence_ids=(item.evidence_id,),
            )
            is None
        )
        assert (
            await resolver.resolve(
                "tenant-a",
                outcome_id=outcome.outcome_id,
                feedback_id=operator_feedback.feedback_id,
                evidence_ids=("evidence-other",),
            )
            is None
        )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_resolver_rejects_duplicate_or_whitespace_references(tmp_path) -> None:
    database, evidence, outcomes, feedback, outcome_repository, item = await _setup(
        tmp_path
    )
    try:
        outcome = await outcomes.finalize(
            "tenant-a",
            "task-learning-1",
            root_cause="supported root",
            supporting_evidence_ids=(item.evidence_id,),
            remediation_recommendation="observe",
            confidence=0.8,
            evidence_sufficient=True,
        )
        operator_feedback = await feedback.record(
            "tenant-a",
            "task-learning-1",
            outcome.outcome_id,
            feedback_type="confirmed",
            idempotency_key="feedback-learning-3",
            recovery=RecoveryObservation(
                observed_at=datetime.now(UTC),
                summary="Pod became Ready",
            ),
        )
        resolver = DurableVerifiedLearningSourceResolver(
            outcome_repository, evidence
        )

        for requested_ids in (
            (item.evidence_id, item.evidence_id),
            (f" {item.evidence_id}",),
            ("",),
        ):
            assert (
                await resolver.resolve(
                    "tenant-a",
                    outcome_id=outcome.outcome_id,
                    feedback_id=operator_feedback.feedback_id,
                    evidence_ids=requested_ids,
                )
                is None
            )
    finally:
        await database.dispose()
