"""Acceptance tests for real Runtime Candidate-vs-Baseline Replay A/B."""

from __future__ import annotations

import importlib.util
from dataclasses import replace
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from athena.api.repositories import Database
from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.api.repositories.skill_evaluation_repository import (
    SkillEvaluationRepository,
)
from athena.api.server import create_app
from athena.application.skill_candidate_service import SkillCandidateService
from athena.application.skill_evaluation_service import SkillEvaluationService
from athena.config import AthenaSettings, DatabaseSettings
from athena.evaluation.candidate_skill_loading import CandidateSkillContextCompiler
from athena.evaluation.skill_replay import (
    fixed_replay_cases,
    replay_case_definition_digest,
)
from athena.evaluation.skill_replay_ab import ReplayABRun, SkillReplayABRunner
from athena.learning.skill_candidate import (
    SkillCandidateLifecycleError,
    TrajectorySkillCandidateProposal,
)
from athena.runtime import (
    AgentRuntime,
    AgentTask,
    Evidence,
    InMemoryRuntimeStore,
    TaskStatus,
    WorkingState,
)
from athena.runtime.learning import TrajectorySummaryBuilder
from athena.runtime.models import utc_now
from athena.runtime.tools import ReadOnlyToolCatalog

REPOSITORY = Path(__file__).parent / "fixtures" / "runtime_repo"
MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260812_0013_skill_replay_ab.py"
)


def _eligible_summary(tenant_id: str, suffix: str):
    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal=f"Diagnose the pricing calculation failure {suffix}",
        repository_root=str(REPOSITORY),
    )
    store.create_task(task)
    runtime = AgentRuntime(store=store)
    for _ in range(4):
        runtime.advance(task.task_id, lease_id=f"replay-ab-source-{suffix}")
    snapshot = store.snapshot(task.task_id)
    assert snapshot.task.status is TaskStatus.SUCCEEDED
    return TrajectorySummaryBuilder().build(snapshot, tenant_id=tenant_id)


async def _candidate_fixture(
    *,
    allowed_tools: tuple[str, ...] = (
        "search_code",
        "read_file_range",
        "get_symbol_outline",
        "run_test",
    ),
    validate: bool = True,
    suffix: str = "a",
):
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    candidate_repository = SkillCandidateRepository(database.session_factory)
    evaluation_repository = SkillEvaluationRepository(database.session_factory)
    candidate_service = SkillCandidateService(candidate_repository)
    summary = _eligible_summary("tenant-a", suffix)
    await candidate_repository.save_trajectory(summary)
    candidate = await candidate_service.propose_from_trajectories(
        TrajectorySkillCandidateProposal(
            tenant_id="tenant-a",
            name=f"pricing-replay-candidate-{suffix}",
            description="Diagnose pricing failures from bounded Runtime Evidence.",
            trigger={
                "task_type": "repository_diagnosis",
                "keywords": ["pricing"],
            },
            allowed_tools=allowed_tools,
            procedure=(
                "Search or inspect the repository with Candidate-approved tools.",
                "Retain source-linked Evidence for every diagnosis step.",
                "Conclude only from read-only Runtime Evidence.",
            ),
            failure_recovery=(
                "Stop after a rejected tool call and retain the failure reason.",
            ),
            success_contract={
                "requires_root_cause": True,
                "requires_evidence": True,
            },
            evidence_requirements=("Evidence must directly support the diagnosis.",),
            token_budget_hint=8_000,
            source_trajectory_ids=(summary.trajectory_id,),
            created_by="replay-ab-test",
            skill_id=f"pricing.replay.{suffix}",
        )
    )
    if validate:
        report = await candidate_service.validate_candidate(
            "tenant-a", candidate.candidate_id
        )
        assert report is not None and report.passed
    service = SkillEvaluationService(
        evaluation_repository,
        candidate_repository=candidate_repository,
        replay_ab_runner=SkillReplayABRunner(REPOSITORY),
    )
    return (
        database,
        candidate_repository,
        evaluation_repository,
        service,
        candidate,
    )


@pytest.mark.asyncio
async def test_candidate_skill_progressive_loading_is_one_shot_and_on_demand() -> None:
    database, _, _, _, candidate = await _candidate_fixture(suffix="loading")
    tools = ReadOnlyToolCatalog().declarations

    unmatched = CandidateSkillContextCompiler(candidate)
    unmatched_context = unmatched.compile(
        task=AgentTask.create(
            goal="Inspect an unrelated inventory module",
            repository_root=str(REPOSITORY),
        ),
        tick_sequence=1,
        working_state=WorkingState(),
        events=(),
        evidence=(),
        tools=tools,
    )
    assert set(unmatched_context.payload["skill_index"]) == {
        "name",
        "description",
        "trigger",
        "risk_level",
    }
    assert "skill_procedure" not in unmatched_context.payload
    assert "skill_reference" not in unmatched_context.payload
    assert unmatched.audit.loaded_layers == ["skill_index"]

    substring_only = CandidateSkillContextCompiler(candidate)
    substring_context = substring_only.compile(
        task=AgentTask.create(
            goal="Inspect an unrelated repricing module",
            repository_root=str(REPOSITORY),
        ),
        tick_sequence=1,
        working_state=WorkingState(),
        events=(),
        evidence=(),
        tools=tools,
    )
    assert "skill_procedure" not in substring_context.payload

    matched = CandidateSkillContextCompiler(candidate)
    matched_task = AgentTask.create(
        goal="Inspect the pricing calculation",
        repository_root=str(REPOSITORY),
    )
    matched_context = matched.compile(
        task=matched_task,
        tick_sequence=1,
        working_state=WorkingState(),
        events=(),
        evidence=(),
        tools=tools,
    )
    assert "skill_procedure" in matched_context.payload
    assert "skill_reference" not in matched_context.payload
    repeated_context = matched.compile(
        task=replace(matched_task, status=TaskStatus.RUNNING),
        tick_sequence=2,
        working_state=WorkingState(),
        events=(),
        evidence=(),
        tools=tools,
    )
    assert "skill_index" not in repeated_context.payload
    assert "skill_procedure" not in repeated_context.payload
    assert matched.audit.injection_count == 1
    assert matched.audit.repeat_injection_avoided_count == 1
    assert matched.audit.injected_tick_sequences == [1]

    reference = CandidateSkillContextCompiler(candidate)
    procedure_steps = candidate.procedure["steps"]
    failure_recovery = candidate.failure_recovery
    evidence_requirements = candidate.evidence_requirements
    reference_context = reference.compile(
        task=AgentTask.create(
            goal="Diagnose the pricing failure and recover safely",
            repository_root=str(REPOSITORY),
        ),
        tick_sequence=1,
        working_state=WorkingState(
            plan=(str(procedure_steps[0]),),
            running_summary=failure_recovery[0],
        ),
        events=(),
        evidence=(
            Evidence(
                evidence_id="evidence-duplicate",
                task_id="task-loading",
                artifact_id="artifact-loading",
                source="tool:search_code",
                summary=evidence_requirements[0],
                created_at=utc_now(),
            ),
        ),
        tools=tools,
    )
    assert "skill_procedure" in reference_context.payload
    assert "skill_reference" in reference_context.payload
    assert len(reference_context.payload["skill_procedure"]["steps"]) == 2
    assert reference_context.payload["skill_reference"]["failure_recovery"] == []
    assert reference_context.payload["skill_reference"]["evidence_requirements"] == []
    assert reference.audit.duplicate_text_omissions == 3
    assert reference.audit.reference_load_count == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_candidate_skill_injection_is_one_shot_per_task() -> None:
    database, _, _, _, candidate = await _candidate_fixture(suffix="task-scope")
    compiler = CandidateSkillContextCompiler(candidate)
    tools = ReadOnlyToolCatalog().declarations

    first_task = AgentTask.create(
        goal="Inspect the pricing calculation",
        repository_root=str(REPOSITORY),
    )
    first_loaded = compiler.compile(
        task=first_task,
        tick_sequence=1,
        working_state=WorkingState(),
        events=(),
        evidence=(),
        tools=tools,
    )
    assert "skill_index" in first_loaded.payload

    first_repeat = compiler.compile(
        task=replace(first_task, status=TaskStatus.RUNNING),
        tick_sequence=2,
        working_state=WorkingState(),
        events=(),
        evidence=(),
        tools=tools,
    )
    assert "skill_index" not in first_repeat.payload

    second_task = AgentTask.create(
        goal="Inspect the pricing calculation",
        repository_root=str(REPOSITORY),
    )
    second_loaded = compiler.compile(
        task=second_task,
        tick_sequence=1,
        working_state=WorkingState(),
        events=(),
        evidence=(),
        tools=tools,
    )
    assert "skill_index" in second_loaded.payload
    assert compiler.audit.injection_count == 2
    assert compiler.audit.repeat_injection_avoided_count == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_replay_ab_executes_both_groups_and_persists_per_case_metrics() -> None:
    database, candidate_repository, _, service, candidate = await _candidate_fixture()

    run = await service.run_replay_ab("tenant-a", candidate.candidate_id)

    assert len(run.comparisons) == 12
    assert run.to_dict()["measurement"] == "runtime_observed"
    assert run.to_dict()["gate"]["source"] == "runtime_observed"
    assert run.to_dict()["baseline_candidate_loaded"] is False
    assert run.to_dict()["candidate_loaded"] is True
    assert set(run.aggregate) == {
        "baseline",
        "candidate",
        "delta",
        "relative_change",
    }
    for comparison in run.comparisons:
        assert comparison.baseline.candidate_loaded is False
        assert comparison.candidate.candidate_loaded is True
        assert comparison.candidate.candidate_read_count > 0
        assert comparison.candidate.candidate_skill_id == candidate.skill_id
        audit = comparison.candidate.candidate_load_audit
        assert audit["injection_count"] == 1
        assert audit["index_read_count"] == 1
        assert audit["repeat_execution_audits_equal"] is True
        assert audit["repeat_injection_avoided_count"] == (
            comparison.candidate.tick_count - 1
        )
        assert comparison.baseline.tick_count > 0
        assert comparison.candidate.tick_count > 0
        assert comparison.baseline.total_tokens > 0
        assert comparison.candidate.total_tokens > 0
        assert comparison.baseline.latency_ms >= 0
        assert comparison.candidate.latency_ms >= 0
        assert comparison.baseline.repeat_count == 2
        assert comparison.candidate.repeat_count == 2
        assert comparison.baseline.repeat_consistent is True
        assert comparison.candidate.repeat_consistent is True
        assert len(comparison.baseline.latency_samples_ms) == 2
        assert len(comparison.candidate.latency_samples_ms) == 2
        assert len(comparison.baseline.execution_digests) == 2
        assert len(comparison.candidate.execution_digests) == 2
        assert comparison.baseline.rollback_passed is True
        assert comparison.candidate.rollback_passed is True
        assert {
            "task_success",
            "root_cause_accuracy",
            "evidence_retention",
            "answer_structure_completeness",
            "tick_count",
            "tool_call_count",
            "input_tokens",
            "output_tokens",
            "total_tokens",
            "latency_ms",
            "retry_count",
            "safety_violations",
            "illegal_tool_executions",
            "timeout_count",
            "human_intervention_count",
        } == set(comparison.deltas)

    candidate_aggregate = run.aggregate["candidate"]
    assert {
        "success_rate",
        "oracle_pass_rate",
        "root_cause_accuracy_rate",
        "evidence_retention_rate",
        "answer_structure_completeness_rate",
        "average_tick_count",
        "average_tool_call_count",
        "average_input_tokens",
        "average_output_tokens",
        "average_total_tokens",
        "average_latency_ms",
        "retry_count",
        "safety_violations",
        "illegal_tool_attempts",
        "illegal_tool_executions",
        "unauthorized_access_attempts",
        "unauthorized_access_successes",
        "high_risk_action_attempts",
        "high_risk_action_successes",
        "injection_attempts",
        "injection_successes",
        "secret_leak_count",
        "timeout_rate",
        "rollback_pass_rate",
        "human_intervention_rate",
        "repeat_consistency_rate",
        "candidate_load_rate",
    } == set(candidate_aggregate)
    assert candidate_aggregate["oracle_pass_rate"] == 1.0
    assert candidate_aggregate["root_cause_accuracy_rate"] == 1.0
    assert candidate_aggregate["answer_structure_completeness_rate"] == 1.0
    assert candidate_aggregate["repeat_consistency_rate"] == 1.0
    assert candidate_aggregate["rollback_pass_rate"] == 1.0
    assert candidate_aggregate["injection_attempts"] == 1.0
    assert candidate_aggregate["injection_successes"] == 0.0
    assert candidate_aggregate["secret_leak_count"] == 0.0
    assert run.gate_checks["total_token_increase_within_5_percent"] is True
    assert run.gate_passed is True
    assert run.status == "passed"
    assert {
        "candidate_parse_success_rate_100",
        "safety_violations_zero",
        "illegal_tool_executions_zero",
        "unauthorized_access_successes_zero",
        "high_risk_action_successes_zero",
        "injection_successes_zero",
        "secret_leaks_zero",
        "success_rate_not_lower",
        "evidence_retention_not_lower",
        "total_token_increase_within_5_percent",
        "average_tick_increase_within_10_percent",
        "tool_call_increase_within_10_percent",
        "critical_cases_all_passed",
        "tool_failure_cases_handled_as_expected",
        "rollback_tests_passed",
        "repeat_consistency_100",
    } == set(run.gate_checks)

    persisted = await service.replay_ab("tenant-a", run.run_id)
    assert persisted == run
    assert await service.replay_ab("tenant-b", run.run_id) is None
    updated = await candidate_repository.get("tenant-a", candidate.candidate_id)
    assert updated is not None
    assert updated.status == "candidate"
    assert updated.evaluation_status == "replay_ab_passed"
    assert updated.online_eligible is False
    assert updated.audit_events[-1]["kind"] == "candidate.replay_ab_evaluated"
    await database.dispose()


@pytest.mark.asyncio
async def test_candidate_failure_and_safety_conflict_are_observed_not_hidden() -> None:
    (
        database,
        candidate_repository,
        _,
        service,
        candidate,
    ) = await _candidate_fixture(
        allowed_tools=("search_code", "read_file_range", "run_test"),
        suffix="restricted",
    )

    run = await service.run_replay_ab("tenant-a", candidate.candidate_id)

    outline = next(
        item for item in run.comparisons if item.case_id == "simple-symbol-outline"
    )
    assert outline.baseline.task_success is True
    assert outline.candidate.task_success is False
    assert outline.candidate.oracle_passed is False
    assert outline.candidate.failure_reason == "CANDIDATE_TOOL_NOT_ALLOWED"
    assert outline.candidate.safety_violations == 1
    assert run.aggregate["candidate"]["safety_violations"] > 0
    assert run.gate_checks["safety_violations_zero"] is False
    assert run.gate_passed is False
    assert run.status == "rejected"
    persisted = await service.replay_ab("tenant-a", run.run_id)
    assert persisted is not None
    persisted_outline = next(
        item
        for item in persisted.comparisons
        if item.case_id == "simple-symbol-outline"
    )
    assert persisted_outline.candidate.task_success is False
    assert persisted_outline.candidate.safety_violations == 1
    updated = await candidate_repository.get("tenant-a", candidate.candidate_id)
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.evaluation_status == "evaluation_failed"
    await database.dispose()


@pytest.mark.asyncio
async def test_replay_ab_is_idempotent_after_gate_rejection() -> None:
    class CountingRunner:
        def __init__(self) -> None:
            self.calls = 0
            self.delegate = SkillReplayABRunner(REPOSITORY)

        def run(self, **kwargs):
            self.calls += 1
            return self.delegate.run(**kwargs)

    database, candidate_repository, evaluation_repository, _, candidate = (
        await _candidate_fixture(suffix="idempotent")
    )
    runner = CountingRunner()
    service = SkillEvaluationService(
        evaluation_repository,
        candidate_repository=candidate_repository,
        replay_ab_runner=runner,
    )

    first = await service.run_replay_ab("tenant-a", candidate.candidate_id)
    second = await service.run_replay_ab("tenant-a", candidate.candidate_id)

    assert first.run_id == second.run_id
    assert first == second
    assert runner.calls == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_new_runner_re_evaluates_same_candidate_rejected_by_old_runner() -> None:
    database, candidate_repository, evaluation_repository, service, candidate = (
        await _candidate_fixture(suffix="runner-upgrade")
    )
    validation = await candidate_repository.latest_validation_for_candidate(
        "tenant-a", candidate.candidate_id
    )
    assert validation is not None
    now = utc_now()
    old_run = ReplayABRun(
        run_id="skill-replay-ab-old-runner-rejection",
        tenant_id="tenant-a",
        candidate_id=candidate.candidate_id,
        candidate_digest=validation.candidate_digest,
        validation_report_id=validation.report_id,
        case_definition_digest=replay_case_definition_digest(fixed_replay_cases()),
        runner="agent-runtime-candidate-ab-v1",
        status="rejected",
        comparisons=(),
        aggregate={},
        gate_checks={"total_token_increase_within_5_percent": False},
        gate_passed=False,
        failure_reason="REPLAY_AB_PUBLICATION_GATE_FAILED",
        started_at=now,
        completed_at=now,
    )
    await evaluation_repository.save_replay_ab(old_run)
    rejected = await candidate_repository.get("tenant-a", candidate.candidate_id)
    assert rejected is not None
    assert rejected.status == "rejected"
    assert rejected.evaluation_status == "evaluation_failed"

    optimized = await service.run_replay_ab("tenant-a", candidate.candidate_id)

    assert optimized.runner == "agent-runtime-candidate-ab-v2"
    assert optimized.gate_passed is True
    updated = await candidate_repository.get("tenant-a", candidate.candidate_id)
    assert updated is not None
    assert updated.status == "candidate"
    assert updated.evaluation_status == "replay_ab_passed"
    assert updated.online_eligible is False
    await database.dispose()


@pytest.mark.asyncio
async def test_replay_ab_requires_validated_candidate_state() -> None:
    database, _, _, service, candidate = await _candidate_fixture(
        validate=False,
        suffix="unvalidated",
    )

    with pytest.raises(SkillCandidateLifecycleError) as exc_info:
        await service.run_replay_ab("tenant-a", candidate.candidate_id)

    assert exc_info.value.error_code == "SKILL_CANDIDATE_VALIDATION_REQUIRED"
    await database.dispose()


@pytest.mark.asyncio
async def test_replay_ab_execution_failure_is_not_reported_as_success() -> None:
    class FailingRunner:
        def run(self, **kwargs):
            raise RuntimeError("provider-like raw failure must not be persisted")

    database, candidate_repository, evaluation_repository, _, candidate = (
        await _candidate_fixture(suffix="runner-failure")
    )
    service = SkillEvaluationService(
        evaluation_repository,
        candidate_repository=candidate_repository,
        replay_ab_runner=FailingRunner(),
    )

    run = await service.run_replay_ab("tenant-a", candidate.candidate_id)

    assert run.status == "evaluation_failed"
    assert run.gate_passed is False
    assert run.comparisons == ()
    assert run.to_dict()["measurement"] == "execution_failed_no_metrics"
    assert run.to_dict()["gate"]["source"] == "fail_closed"
    assert run.failure_reason == "REPLAY_AB_EXECUTION_FAILED"
    assert "provider-like" not in str(run.to_dict())
    updated = await candidate_repository.get("tenant-a", candidate.candidate_id)
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.evaluation_status == "evaluation_failed"
    assert updated.online_eligible is False
    await database.dispose()


def test_replay_ab_api_executes_and_returns_idempotent_report() -> None:
    settings = AthenaSettings(
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:", auto_migrate=True)
    )
    with TestClient(create_app(settings=settings)) as client:
        task = client.post(
            "/api/runtime/tasks",
            json={
                "goal": "Diagnose the pricing calculation failure",
                "repository_path": str(REPOSITORY),
                "profile": "standard",
            },
        ).json()["data"]
        run_task = client.post(f"/api/runtime/tasks/{task['id']}/run")
        assert run_task.status_code == 200, run_task.text
        trajectory_response = client.post(
            f"/api/runtime/skills/tasks/{task['id']}/trajectory"
        )
        assert trajectory_response.status_code == 200, trajectory_response.text
        trajectory = trajectory_response.json()["data"]

        candidate_response = client.post(
            "/api/skill-candidates/from-trajectories",
            json={
                "name": "api-replay-ab-candidate",
                "description": "Evaluate a bounded read-only Candidate.",
                "trigger": {
                    "task_type": "repository_diagnosis",
                    "keywords": ["pricing"],
                },
                "allowed_tools": [
                    "search_code",
                    "read_file_range",
                    "get_symbol_outline",
                    "run_test",
                ],
                "procedure": [
                    "Inspect the repository with approved read-only tools.",
                    "Retain Evidence for every diagnosis step.",
                ],
                "failure_recovery": ["Stop and retain the rejection reason."],
                "success_contract": {
                    "requires_root_cause": True,
                    "requires_evidence": True,
                },
                "evidence_requirements": ["Evidence must support the diagnosis."],
                "token_budget_hint": 8000,
                "source_trajectory_ids": [trajectory["trajectory_id"]],
                "version": 1,
                "risk_level": "S1",
            },
        )
        assert candidate_response.status_code == 201, candidate_response.text
        candidate = candidate_response.json()["data"]
        validation = client.post(f"/api/skill-candidates/{candidate['id']}/validate")
        assert validation.status_code == 200, validation.text
        assert validation.json()["data"]["passed"] is True

        replay_response = client.post(
            "/api/skill-evaluation/candidates/" f"{candidate['id']}/replay-ab-runs"
        )
        assert replay_response.status_code == 201, replay_response.text
        replay = replay_response.json()["data"]
        assert replay["case_count"] == 12
        assert replay["baseline_candidate_loaded"] is False
        assert replay["candidate_loaded"] is True
        assert replay["gate"]["activation_allowed"] is False
        loaded = client.get(f"/api/skill-evaluation/replay-ab-runs/{replay['run_id']}")
        assert loaded.status_code == 200
        assert loaded.json()["data"]["run_id"] == replay["run_id"]
        repeated = client.post(
            "/api/skill-evaluation/candidates/" f"{candidate['id']}/replay-ab-runs"
        )
        assert repeated.status_code == 201
        assert repeated.json()["data"]["run_id"] == replay["run_id"]


def test_replay_ab_migration_upgrade_and_downgrade() -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_replay_ab_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite:///:memory:")

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

    inspector = inspect(engine)
    assert "skill_replay_ab_runs" in inspector.get_table_names()
    columns = {item["name"] for item in inspector.get_columns("skill_replay_ab_runs")}
    assert {
        "candidate_digest",
        "comparisons_json",
        "aggregate_json",
        "gate_checks_json",
        "gate_passed",
    }.issubset(columns)
    assert any(
        item["name"] == "uq_skill_replay_ab_identity"
        for item in inspector.get_unique_constraints("skill_replay_ab_runs")
    )

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
    assert "skill_replay_ab_runs" not in inspect(engine).get_table_names()
    engine.dispose()
