"""P0 vertical-slice tests for trajectory admission and Skill Candidates."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from athena.api.repositories import Database
from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.application.skill_candidate_service import SkillCandidateService
from athena.config import DatabaseSettings
from athena.learning.skill_candidate import (
    CANDIDATE_STATUS,
    SkillCandidateSourceError,
    TrajectorySkillCandidateProposal,
)
from athena.runtime import AgentRuntime, AgentTask, InMemoryRuntimeStore, TaskStatus
from athena.runtime.models import Event, FinalReport, TaskBudget, utc_now
from athena.runtime.learning import (
    TrajectoryRejectionReason,
    TrajectoryStatus,
    TrajectorySummaryBuilder,
)


MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260812_0010_skill_trajectory_admission.py"
)


def _completed_snapshot():
    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal="Diagnose the pricing calculation failure",
        repository_root=str(Path(__file__).parent / "fixtures" / "runtime_repo"),
    )
    store.create_task(task)
    runtime = AgentRuntime(store=store)
    for _ in range(4):
        runtime.advance(task.task_id, lease_id="trajectory-test-worker")
    snapshot = store.snapshot(task.task_id)
    assert snapshot.task.status is TaskStatus.SUCCEEDED
    return snapshot


def _proposal(trajectory_id: str) -> TrajectorySkillCandidateProposal:
    return TrajectorySkillCandidateProposal(
        tenant_id="tenant-a",
        name="repository-pricing-diagnosis",
        description="Diagnose repeatable pricing failures from repository evidence.",
        trigger={"task_type": "repository_diagnosis", "keywords": ["pricing"]},
        allowed_tools=("search_code", "read_file_range", "run_test"),
        procedure=(
            "Search for the failing calculation entry point.",
            "Read the bounded implementation and collect Evidence.",
            "Run the allowlisted repository check and compare the result.",
        ),
        failure_recovery=(
            "Stop after a rejected tool call and request human review.",
        ),
        success_contract={
            "requires_root_cause": True,
            "requires_evidence": True,
        },
        evidence_requirements=(
            "At least one source-linked Evidence item supports the conclusion.",
        ),
        token_budget_hint=8_000,
        source_trajectory_ids=(trajectory_id,),
        created_by="curator-a",
    )


def test_summary_is_redacted_bounded_and_explainably_eligible() -> None:
    snapshot = _completed_snapshot()
    snapshot = replace(
        snapshot,
        task=replace(
            snapshot.task,
            goal=(
                "Diagnose D:\\private\\repo failure with api_key=do-not-store "
                "for owner@example.com"
            ),
        ),
    )
    unsafe = replace(
        snapshot.evidence[0],
        summary="Read D:\\private\\repo\\pricing.py with password=hunter2.",
    )
    snapshot = replace(snapshot, evidence=(unsafe, *snapshot.evidence[1:]))

    summary = TrajectorySummaryBuilder().build(snapshot, tenant_id="tenant-a")

    assert summary.status is TrajectoryStatus.ELIGIBLE
    assert summary.admission.eligible is True
    assert summary.admission.rejection_reasons == ()
    assert 0.0 <= summary.admission.quality_score <= 1.0
    assert set(summary.admission.quality_factors) == {
        "task_success",
        "evidence_completeness",
        "tool_efficiency",
        "reusability",
        "safety_stability",
    }
    serialized = repr(summary.to_dict()).lower()
    assert "do-not-store" not in serialized
    assert "hunter2" not in serialized
    assert "owner@example.com" not in serialized
    assert "d:\\private" not in serialized
    assert "repository_root" not in serialized
    assert "arguments" not in serialized
    assert "artifact_id" not in serialized
    assert summary.contains_raw_artifacts is False
    assert summary.contains_hidden_reasoning is False


def test_admission_reports_every_mandatory_rejection_reason() -> None:
    snapshot = _completed_snapshot()
    task = replace(
        snapshot.task,
        status=TaskStatus.FAILED,
        budget=TaskBudget(
            total_tokens=10,
            max_ticks=1,
            consumed_tokens=100,
        ),
        final_report=FinalReport(
            root_cause="unverified",
            repair_recommendation="none",
            evidence_ids=("missing-evidence",),
        ),
    )
    security_event = Event(
        event_id="security-event",
        task_id=task.task_id,
        tick_id="",
        sequence=999,
        kind="security.violation",
        payload={"security_violation": True},
        created_at=utc_now(),
    )
    overreach_event = Event(
        event_id="overreach-event",
        task_id=task.task_id,
        tick_id="",
        sequence=1000,
        kind="tool.rejected",
        payload={"reason_code": "UNKNOWN_TOOL"},
        created_at=utc_now(),
    )

    summary = TrajectorySummaryBuilder().build(
        replace(snapshot, task=task, events=(*snapshot.events, security_event, overreach_event)),
        tenant_id="tenant-a",
    )

    assert summary.status is TrajectoryStatus.REJECTED
    assert set(summary.admission.rejection_reasons) == {
        TrajectoryRejectionReason.TASK_NOT_SUCCEEDED.value,
        TrajectoryRejectionReason.EVIDENCE_INCOMPLETE.value,
        TrajectoryRejectionReason.SECURITY_VIOLATION.value,
        TrajectoryRejectionReason.TOOL_OVERREACH.value,
        TrajectoryRejectionReason.BUDGET_EXCEEDED.value,
    }


@pytest.mark.asyncio
async def test_eligible_trajectory_persists_then_creates_candidate_only() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = SkillCandidateRepository(database.session_factory)
    summary = TrajectorySummaryBuilder().build(
        _completed_snapshot(), tenant_id="tenant-a"
    )

    persisted = await repository.save_trajectory(summary)
    duplicate = await repository.save_trajectory(summary)
    assert duplicate == persisted
    events = await repository.list_trajectory_events(
        "tenant-a", summary.trajectory_id
    )
    assert [(item["from_status"], item["to_status"]) for item in events] == [
        (None, "observed"),
        ("observed", "eligible"),
    ]

    service = SkillCandidateService(repository)
    candidate = await service.propose_from_trajectories(
        _proposal(summary.trajectory_id)
    )

    assert candidate.status == CANDIDATE_STATUS
    assert candidate.online_eligible is False
    assert candidate.skill_id.startswith("candidate-skill-")
    assert candidate.version == 1
    assert candidate.description
    assert candidate.trigger == {
        "task_type": "repository_diagnosis",
        "keywords": ["pricing"],
    }
    assert candidate.allowed_tools == (
        "search_code",
        "read_file_range",
        "run_test",
    )
    assert candidate.procedure["steps"]
    assert candidate.failure_recovery
    assert candidate.success_contract == {
        "requires_root_cause": True,
        "requires_evidence": True,
    }
    assert candidate.evidence_requirements
    assert candidate.token_budget_hint == 8_000
    assert candidate.source_trajectory_ids == (summary.trajectory_id,)
    assert candidate.evaluation_status == "not_evaluated"
    assert candidate.risk_level == "S1"
    assert candidate.manifest["activation_allowed"] is False
    assert candidate.audit_events[0]["to_status"] == CANDIDATE_STATUS
    await database.dispose()


@pytest.mark.asyncio
async def test_rejected_trajectory_and_non_readonly_tool_cannot_create_candidate() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = SkillCandidateRepository(database.session_factory)
    snapshot = _completed_snapshot()
    rejected = TrajectorySummaryBuilder().build(
        replace(snapshot, task=replace(snapshot.task, status=TaskStatus.FAILED)),
        tenant_id="tenant-a",
    )
    await repository.save_trajectory(rejected)
    service = SkillCandidateService(repository)

    with pytest.raises(SkillCandidateSourceError) as not_eligible:
        await service.propose_from_trajectories(_proposal(rejected.trajectory_id))
    assert (
        not_eligible.value.error_code
        == "SKILL_CANDIDATE_TRAJECTORY_NOT_ELIGIBLE"
    )

    eligible = TrajectorySummaryBuilder().build(
        snapshot, tenant_id="tenant-a"
    )
    await repository.save_trajectory(eligible)
    unsafe = replace(_proposal(eligible.trajectory_id), allowed_tools=("write_file",))
    with pytest.raises(SkillCandidateSourceError) as forbidden_tool:
        await service.propose_from_trajectories(unsafe)
    assert (
        forbidden_tool.value.error_code
        == "SKILL_CANDIDATE_TOOL_NOT_READONLY_ALLOWLISTED"
    )
    await database.dispose()


def test_migration_persists_trajectory_schema_and_denies_active_candidate() -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_trajectory_admission_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table(
        "skill_candidates",
        metadata,
        Column("id", String(96), primary_key=True),
        Column("status", String(32), nullable=False),
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

    inspector = inspect(engine)
    assert "learning_trajectories" in inspector.get_table_names()
    assert "learning_trajectory_events" in inspector.get_table_names()
    candidate_columns = {
        item["name"] for item in inspector.get_columns("skill_candidates")
    }
    assert {
        "skill_id",
        "version",
        "allowed_tools_json",
        "source_trajectory_ids_json",
        "evaluation_status",
        "risk_level",
    }.issubset(candidate_columns)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO skill_candidates (id, status) "
                    "VALUES ('candidate-active-denied', 'active')"
                )
            )

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
    assert "learning_trajectories" not in inspect(engine).get_table_names()
    engine.dispose()
