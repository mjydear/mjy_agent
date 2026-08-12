from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.responses import JSONResponse

from athena.api.repositories import (
    Database,
    EvidenceRepository,
    TaskCreate,
    TaskRepository,
)
from athena.api.services import ApiServiceError
from athena.application.diagnosis_outcome_service import (
    DiagnosisOutcomeService,
    DiagnosisOutcomeServiceError,
)
from athena.application.operator_feedback_service import (
    OperatorFeedbackService,
    OperatorFeedbackServiceError,
    RecoveryObservation,
)
from athena.api.repositories.diagnosis_outcome_repository import (
    DiagnosisOutcomeRepository,
)
from athena.api.routes.diagnosis_outcomes import router
from athena.config import DatabaseSettings
from athena.infra.evidence_content import LocalEvidenceContentStore


def _task(task_id: str = "task-outcome-1", tenant_id: str = "tenant-a") -> TaskCreate:
    return TaskCreate(
        task_id=task_id,
        tenant_id=tenant_id,
        objective="diagnose payment pod",
        environment_id="env-prod",
        environment_mode="mock",
        scope={"namespace": "payment"},
        policy_snapshot={"readonly": True, "version": "policy-v1"},
        config_snapshot={"model": "rules-only", "tool_set": "k8s-readonly-v1"},
        budget={"remaining_steps": 4, "remaining_tokens": 6000},
        execution_profile="bounded_policy_loop",
    )


async def _setup(tmp_path, *, task_id: str = "task-outcome-1"):
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    tasks = TaskRepository(database.session_factory)
    await tasks.create_task(_task(task_id))
    evidences = EvidenceRepository(
        database.session_factory,
        LocalEvidenceContentStore(tmp_path / "evidence", max_content_bytes=4096),
    )
    evidence = await evidences.create(
        tenant_id="tenant-a",
        task_id=task_id,
        evidence_type="resource_snapshot",
        source="k8s.pod.describe",
        data_origin="mock",
        summary="pod has restarted repeatedly",
        content={"reason": "CrashLoopBackOff"},
        observed_at=datetime(2026, 8, 9, 10, 0, tzinfo=UTC),
    )
    outcomes = DiagnosisOutcomeRepository(database.session_factory)
    return (
        database,
        DiagnosisOutcomeService(outcomes),
        OperatorFeedbackService(outcomes),
        evidence,
    )


@pytest.mark.asyncio
async def test_finalize_persists_supported_outcome_without_prompt_or_hidden_thought(
    tmp_path,
) -> None:
    database, outcomes, _, evidence = await _setup(tmp_path)
    try:
        outcome = await outcomes.finalize(
            "tenant-a",
            "task-outcome-1",
            root_cause="容器启动后因配置错误退出",
            supporting_evidence_ids=(evidence.evidence_id,),
            remediation_recommendation="校验并修正 Deployment 的环境变量配置后重新发布",
            confidence=0.92,
            evidence_sufficient=True,
        )

        assert outcome.outcome_id.startswith("outcome-")
        assert outcome.root_cause == "容器启动后因配置错误退出"
        assert outcome.supporting_evidence_ids == (evidence.evidence_id,)
        assert outcome.remediation_recommendation.startswith("校验并修正")
        assert outcome.confidence == pytest.approx(0.92)
        assert outcome.evidence_sufficient is True
        assert not hasattr(outcome, "raw_prompt")
        assert not hasattr(outcome, "hidden_thought")

        persisted = await outcomes.get("tenant-a", outcome.outcome_id)
        assert persisted is not None
        assert persisted.outcome_id == outcome.outcome_id
        assert not hasattr(persisted, "raw_prompt")
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_finalize_rejects_insufficient_claim_and_can_record_non_claim_outcome(
    tmp_path,
) -> None:
    database, outcomes, _, _ = await _setup(tmp_path)
    try:
        with pytest.raises(DiagnosisOutcomeServiceError) as exc_info:
            await outcomes.finalize(
                "tenant-a",
                "task-outcome-1",
                root_cause="没有证据支持的猜测",
                supporting_evidence_ids=(),
                remediation_recommendation="立即修改生产配置",
                confidence=0.4,
                evidence_sufficient=False,
            )
        assert exc_info.value.error_code == "OUTCOME_EVIDENCE_INSUFFICIENT"

        outcome = await outcomes.finalize(
            "tenant-a",
            "task-outcome-1",
            root_cause=None,
            supporting_evidence_ids=(),
            remediation_recommendation=None,
            confidence=0.0,
            evidence_sufficient=False,
        )
        assert outcome.evidence_sufficient is False
        assert outcome.root_cause is None
        assert outcome.remediation_recommendation is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_finalize_and_feedback_hide_cross_tenant_records(tmp_path) -> None:
    database, outcomes, feedback, evidence = await _setup(tmp_path)
    try:
        with pytest.raises(DiagnosisOutcomeServiceError) as outcome_error:
            await outcomes.finalize(
                "tenant-b",
                "task-outcome-1",
                root_cause="cross tenant",
                supporting_evidence_ids=(evidence.evidence_id,),
                remediation_recommendation="do not persist",
                confidence=0.9,
                evidence_sufficient=True,
            )
        assert outcome_error.value.error_code == "DIAGNOSTIC_TASK_NOT_FOUND"

        outcome = await outcomes.finalize(
            "tenant-a",
            "task-outcome-1",
            root_cause="validated root cause",
            supporting_evidence_ids=(evidence.evidence_id,),
            remediation_recommendation="collect operator confirmation",
            confidence=0.8,
            evidence_sufficient=True,
        )
        with pytest.raises(OperatorFeedbackServiceError) as feedback_error:
            await feedback.record(
                "tenant-b",
                "task-outcome-1",
                outcome.outcome_id,
                feedback_type="confirmed",
                idempotency_key="feedback-cross-tenant",
            )
        assert feedback_error.value.error_code == "DIAGNOSIS_OUTCOME_NOT_FOUND"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_finalize_is_idempotent_and_conflicting_replay_is_rejected(tmp_path) -> None:
    database, outcomes, _, evidence = await _setup(tmp_path)
    try:
        arguments = {
            "root_cause": "image registry credentials are invalid",
            "supporting_evidence_ids": (evidence.evidence_id,),
            "remediation_recommendation": "refresh the registry credential reference",
            "confidence": 0.75,
            "evidence_sufficient": True,
        }
        first = await outcomes.finalize("tenant-a", "task-outcome-1", **arguments)
        replay = await outcomes.finalize(
            "tenant-a",
            "task-outcome-1",
            **{**arguments, "supporting_evidence_ids": (evidence.evidence_id,)},
        )
        assert replay.outcome_id == first.outcome_id

        with pytest.raises(DiagnosisOutcomeServiceError) as exc_info:
            await outcomes.finalize(
                "tenant-a",
                "task-outcome-1",
                **{
                    **arguments,
                    "root_cause": "a different unsupported cause",
                },
            )
        assert exc_info.value.error_code == "DIAGNOSIS_OUTCOME_CONFLICT"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_identical_finalization_returns_one_fact(tmp_path) -> None:
    database, outcomes, _, evidence = await _setup(tmp_path)
    try:
        async def finalize():
            return await outcomes.finalize(
                "tenant-a",
                "task-outcome-1",
                root_cause="one supported cause",
                supporting_evidence_ids=(evidence.evidence_id,),
                remediation_recommendation="observe the workload before proposing a write",
                confidence=0.7,
                evidence_sufficient=True,
            )

        results = await asyncio.gather(finalize(), finalize())
        assert results[0].outcome_id == results[1].outcome_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_get_feedback_is_scoped_by_tenant_task_and_outcome(tmp_path) -> None:
    database, outcomes, feedback, evidence = await _setup(tmp_path)
    try:
        outcome = await outcomes.finalize(
            "tenant-a",
            "task-outcome-1",
            root_cause="supported cause",
            supporting_evidence_ids=(evidence.evidence_id,),
            remediation_recommendation="observe before an approved write",
            confidence=0.81,
            evidence_sufficient=True,
        )
        recorded = await feedback.record(
            "tenant-a",
            "task-outcome-1",
            outcome.outcome_id,
            feedback_type="confirmed",
            idempotency_key="feedback-learning-1",
        )
        repository = DiagnosisOutcomeRepository(database.session_factory)

        assert await repository.get_feedback(
            "tenant-a", "task-outcome-1", outcome.outcome_id
        ) == (recorded,)
        assert await repository.get_feedback(
            "tenant-b", "task-outcome-1", outcome.outcome_id
        ) == ()
        assert await repository.get_feedback(
            "tenant-a", "task-other", outcome.outcome_id
        ) == ()
        assert await repository.get_feedback(
            "tenant-a", "task-outcome-1", "outcome-other"
        ) == ()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_feedback_supports_confirmation_correction_rejection_and_recovery(
    tmp_path,
) -> None:
    database, outcomes, feedback, evidence = await _setup(tmp_path)
    try:
        outcome = await outcomes.finalize(
            "tenant-a",
            "task-outcome-1",
            root_cause="probe configuration causes process exit",
            supporting_evidence_ids=(evidence.evidence_id,),
            remediation_recommendation="restore the known-good probe configuration",
            confidence=0.88,
            evidence_sufficient=True,
        )
        recovered_at = datetime(2026, 8, 9, 10, 15, tzinfo=UTC)
        confirmed = await feedback.record(
            "tenant-a",
            "task-outcome-1",
            outcome.outcome_id,
            feedback_type="confirmed",
            operator_id="operator-1",
            idempotency_key="feedback-confirm-1",
            recovery=RecoveryObservation(
                observed_at=recovered_at,
                summary="Pod remained ready for five minutes after the change",
            ),
        )
        assert confirmed.feedback_type == "confirmed"
        assert confirmed.recovery is not None
        assert confirmed.recovery.task_id == "task-outcome-1"
        assert confirmed.recovery.outcome_id == outcome.outcome_id
        assert confirmed.recovery.summary.startswith("Pod remained ready")

        replay = await feedback.record(
            "tenant-a",
            "task-outcome-1",
            outcome.outcome_id,
            feedback_type="confirmed",
            operator_id="operator-1",
            idempotency_key="feedback-confirm-1",
            recovery=RecoveryObservation(
                observed_at=recovered_at,
                summary="Pod remained ready for five minutes after the change",
            ),
        )
        assert replay.feedback_id == confirmed.feedback_id
        assert replay.recovery is not None
        assert replay.recovery.recovery_id == confirmed.recovery.recovery_id

        corrected = await feedback.record(
            "tenant-a",
            "task-outcome-1",
            outcome.outcome_id,
            feedback_type="corrected",
            operator_id="operator-2",
            idempotency_key="feedback-correct-1",
            corrected_root_cause="the registry endpoint was unavailable",
            note="The probe was healthy in the same window",
        )
        assert corrected.corrected_root_cause == "the registry endpoint was unavailable"

        rejected = await feedback.record(
            "tenant-a",
            "task-outcome-1",
            outcome.outcome_id,
            feedback_type="rejected",
            operator_id="operator-3",
            idempotency_key="feedback-reject-1",
            note="Evidence was collected from the wrong namespace",
        )
        assert rejected.feedback_type == "rejected"
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_feedback_idempotency_conflict_and_invalid_correction_are_explicit(
    tmp_path,
) -> None:
    database, outcomes, feedback, evidence = await _setup(tmp_path)
    try:
        outcome = await outcomes.finalize(
            "tenant-a",
            "task-outcome-1",
            root_cause="supported cause",
            supporting_evidence_ids=(evidence.evidence_id,),
            remediation_recommendation="read-only observation",
            confidence=0.81,
            evidence_sufficient=True,
        )
        await feedback.record(
            "tenant-a",
            "task-outcome-1",
            outcome.outcome_id,
            feedback_type="confirmed",
            operator_id="operator-1",
            idempotency_key="feedback-idem-1",
        )
        with pytest.raises(OperatorFeedbackServiceError) as conflict:
            await feedback.record(
                "tenant-a",
                "task-outcome-1",
                outcome.outcome_id,
                feedback_type="rejected",
                operator_id="operator-1",
                idempotency_key="feedback-idem-1",
            )
        assert conflict.value.error_code == "FEEDBACK_IDEMPOTENCY_CONFLICT"

        with pytest.raises(OperatorFeedbackServiceError) as invalid:
            await feedback.record(
                "tenant-a",
                "task-outcome-1",
                outcome.outcome_id,
                feedback_type="corrected",
                operator_id="operator-1",
                idempotency_key="feedback-invalid-1",
            )
        assert invalid.value.error_code == "FEEDBACK_CORRECTION_REQUIRED"
    finally:
        await database.dispose()


def test_diagnosis_routes_reject_missing_app_state_dependency() -> None:
    app = FastAPI()

    @app.exception_handler(ApiServiceError)
    async def handle_error(_, exc: ApiServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error_code": exc.error_code, "message": exc.message},
        )

    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/api/diagnosis-outcomes/tasks/task-outcome-1/finalize",
            json={
                "root_cause": "x",
                "task_id": "task-outcome-1",
                "supporting_evidence_ids": [],
                "remediation_recommendation": "y",
                "confidence": 0.5,
                "evidence_sufficient": True,
            },
        )
    assert response.status_code == 503
    assert response.json()["error_code"] == "DIAGNOSIS_OUTCOME_SERVICE_UNAVAILABLE"
