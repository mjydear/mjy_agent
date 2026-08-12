"""P0 Candidate validation and fixed Skill Baseline acceptance tests."""

from __future__ import annotations

from collections import Counter
from dataclasses import replace
import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from fastapi.testclient import TestClient
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect

from athena.agent.policy.contracts import RiskLevel, ToolSpecV2
from athena.api.repositories import Database
from athena.api.repositories.skill_candidate_repository import (
    SkillCandidateRepository,
)
from athena.api.repositories.skill_evaluation_repository import (
    SkillEvaluationRepository,
)
from athena.api.server import create_app
from athena.application.skill_candidate_service import SkillCandidateService
from athena.application.skill_candidate_validator import SkillCandidateValidator
from athena.config import AthenaSettings, DatabaseSettings
from athena.evaluation.skill_replay import (
    ReplayCaseCategory,
    SkillBaselineRunner,
    fixed_replay_cases,
)
from athena.learning.skill_candidate import (
    SkillCandidateLifecycleError,
    TrajectorySkillCandidateProposal,
)
from athena.runtime import AgentRuntime, AgentTask, InMemoryRuntimeStore, TaskStatus
from athena.runtime.learning import TrajectorySummaryBuilder
from athena.runtime.tools import ReadOnlyToolCatalog

REPOSITORY = Path(__file__).parent / "fixtures" / "runtime_repo"
MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260812_0011_skill_validation_baseline.py"
)


def _eligible_summary():
    store = InMemoryRuntimeStore()
    task = AgentTask.create(
        goal="Diagnose the pricing calculation failure",
        repository_root=str(REPOSITORY),
    )
    store.create_task(task)
    runtime = AgentRuntime(store=store)
    for _ in range(4):
        runtime.advance(task.task_id, lease_id="validation-test-worker")
    snapshot = store.snapshot(task.task_id)
    assert snapshot.task.status is TaskStatus.SUCCEEDED
    return TrajectorySummaryBuilder().build(snapshot, tenant_id="tenant-a")


def _proposal(trajectory_id: str) -> TrajectorySkillCandidateProposal:
    return TrajectorySkillCandidateProposal(
        tenant_id="tenant-a",
        name="validated-pricing-diagnosis",
        description="Diagnose pricing failures from bounded repository Evidence.",
        trigger={"task_type": "repository_diagnosis", "keywords": ["pricing"]},
        allowed_tools=("search_code", "read_file_range", "run_test"),
        procedure=(
            "Search for the calculation entry point.",
            "Read bounded source and retain Evidence.",
            "Run the allowlisted repository check.",
        ),
        failure_recovery=("Stop after a rejected tool call and request human review.",),
        success_contract={
            "requires_root_cause": True,
            "requires_evidence": True,
        },
        evidence_requirements=("Source-linked Evidence must support the root cause.",),
        token_budget_hint=8_000,
        source_trajectory_ids=(trajectory_id,),
        created_by="curator-a",
    )


@pytest.mark.asyncio
async def test_candidate_validation_passes_and_persists_auditable_report() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = SkillCandidateRepository(database.session_factory)
    summary = _eligible_summary()
    await repository.save_trajectory(summary)
    service = SkillCandidateService(repository)
    candidate = await service.propose_from_trajectories(
        _proposal(summary.trajectory_id)
    )

    with pytest.raises(SkillCandidateLifecycleError) as validation_required:
        await service.mark_replay_pending("tenant-a", candidate.candidate_id)
    assert validation_required.value.error_code == "SKILL_CANDIDATE_VALIDATION_REQUIRED"

    report = await service.validate_candidate("tenant-a", candidate.candidate_id)

    assert report is not None
    assert report.passed is True
    assert report.schema_valid is True
    assert report.security_valid is True
    assert all(report.checks.values())
    assert report.violations == ()
    assert report.to_dict()["activation_allowed"] is False
    persisted = await service.get_validation("tenant-a", report.report_id)
    assert persisted == report
    updated = await repository.get("tenant-a", candidate.candidate_id)
    assert updated is not None
    assert updated.status == "candidate"
    assert updated.evaluation_status == "validation_passed"
    assert updated.online_eligible is False
    assert updated.audit_events[-1]["kind"] == "candidate.validated"
    replay_pending = await service.mark_replay_pending(
        "tenant-a", candidate.candidate_id
    )
    assert replay_pending is not None
    assert replay_pending.status == "replay_pending"
    assert replay_pending.online_eligible is False
    await database.dispose()


@pytest.mark.asyncio
async def test_validator_rejects_unknown_write_and_server_controlled_arguments() -> (
    None
):
    summary = _eligible_summary()
    catalog_specs = {
        item.name: item.as_spec() for item in ReadOnlyToolCatalog().declarations
    }
    catalog_specs["write_file"] = ToolSpecV2(
        name="write_file",
        version="1.0.0",
        domain="repository",
        input_schema={
            "type": "object",
            "properties": {"relative_path": {"type": "string"}},
            "required": ["relative_path"],
            "additionalProperties": False,
        },
        output_schema={"type": "object"},
        required_capabilities=("repository.write",),
        risk_level=RiskLevel.S3,
        readonly=False,
        idempotent=False,
        timeout_seconds=5,
    )

    class Source:
        async def get_trajectory(self, tenant_id: str, trajectory_id: str):
            if tenant_id == "tenant-a" and trajectory_id == summary.trajectory_id:
                return summary
            return None

    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = SkillCandidateRepository(database.session_factory)
    await repository.save_trajectory(summary)
    candidate = await SkillCandidateService(repository).propose_from_trajectories(
        _proposal(summary.trajectory_id)
    )

    unknown = replace(candidate, allowed_tools=("unknown_tool",))
    unknown_report = await SkillCandidateValidator().validate(unknown, Source())
    assert "CANDIDATE_UNKNOWN_TOOL" in {item.code for item in unknown_report.violations}

    write = replace(candidate, allowed_tools=("write_file",))
    write_report = await SkillCandidateValidator(tool_specs=catalog_specs).validate(
        write, Source()
    )
    assert {
        "CANDIDATE_WRITE_TOOL_FORBIDDEN",
        "CANDIDATE_TOOL_CAPABILITY_FORBIDDEN",
        "CANDIDATE_TOOL_RISK_FORBIDDEN",
    }.issubset({item.code for item in write_report.violations})

    overreach = replace(
        candidate,
        procedure={
            **candidate.procedure,
            "tool_calls": [
                {
                    "tool_name": "read_file_range",
                    "arguments": {
                        "relative_path": "pricing.py",
                        "repository_root": "server-owned",
                    },
                }
            ],
        },
    )
    overreach_report = await SkillCandidateValidator().validate(overreach, Source())
    assert "CANDIDATE_SERVER_ARGUMENT_FORBIDDEN" in {
        item.code for item in overreach_report.violations
    }
    assert overreach_report.security_valid is False
    await database.dispose()


@pytest.mark.asyncio
async def test_validator_rejects_schema_contract_source_and_invisible_text() -> None:
    summary = _eligible_summary()

    class Source:
        async def get_trajectory(self, tenant_id: str, trajectory_id: str):
            if tenant_id == "tenant-a" and trajectory_id == summary.trajectory_id:
                return summary
            return None

    class MissingSource:
        async def get_trajectory(self, tenant_id: str, trajectory_id: str):
            return None

    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = SkillCandidateRepository(database.session_factory)
    await repository.save_trajectory(summary)
    candidate = await SkillCandidateService(repository).propose_from_trajectories(
        _proposal(summary.trajectory_id)
    )
    validator = SkillCandidateValidator()

    invalid_schema = await validator.validate(
        replace(candidate, schema_version="unsupported", version=0), Source()
    )
    assert {
        "CANDIDATE_SCHEMA_VERSION_UNSUPPORTED",
        "CANDIDATE_VERSION_INVALID",
    }.issubset({item.code for item in invalid_schema.violations})

    invalid_contract = await validator.validate(
        replace(candidate, success_contract={}), Source()
    )
    assert "CANDIDATE_SUCCESS_CONTRACT_INVALID" in {
        item.code for item in invalid_contract.violations
    }

    invalid_source = await validator.validate(candidate, MissingSource())
    assert "CANDIDATE_SOURCE_TRAJECTORY_INVALID" in {
        item.code for item in invalid_source.violations
    }

    invisible = await validator.validate(
        replace(candidate, description="diagnosis\u202eunsafe"), Source()
    )
    assert "CANDIDATE_INVISIBLE_CONTROL_CHARACTER" in {
        item.code for item in invisible.violations
    }
    await database.dispose()


@pytest.mark.asyncio
async def test_failed_validation_moves_persisted_candidate_only_to_rejected() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = SkillCandidateRepository(database.session_factory)
    summary = _eligible_summary()
    await repository.save_trajectory(summary)
    candidate = await repository.create_or_get(
        candidate_id="unsafe-candidate",
        tenant_id="tenant-a",
        name="unsafe-candidate",
        workflow_type="repository_diagnosis",
        environment_type="repository",
        capabilities=("repository.read",),
        manifest={
            "schema_version": "athena.skill-candidate.v1",
            "candidate_only": True,
            "creates_tool": False,
            "readonly": True,
            "activation_allowed": False,
        },
        procedure={"steps": ["Use only governed Evidence."]},
        source_outcome_id=f"trajectory:{summary.trajectory_id}",
        source_feedback_id="trajectory-admission",
        evidence_ids=tuple(item["evidence_id"] for item in summary.evidence),
        source_digest="unsafe-candidate-digest",
        source_summary={"source_type": "eligible_runtime_trajectories"},
        created_by="curator-a",
        description="Candidate with an intentionally unknown tool.",
        trigger={"task_type": "repository_diagnosis"},
        allowed_tools=("write_file",),
        failure_recovery=("Stop after policy rejection.",),
        success_contract={"requires_evidence": True},
        evidence_requirements=("Retain source-linked Evidence.",),
        token_budget_hint=1_000,
        source_trajectory_ids=(summary.trajectory_id,),
    )

    report = await SkillCandidateService(repository).validate_candidate(
        "tenant-a", candidate.candidate_id
    )

    assert report is not None and report.passed is False
    updated = await repository.get("tenant-a", candidate.candidate_id)
    assert updated is not None
    assert updated.status == "rejected"
    assert updated.evaluation_status == "validation_failed"
    assert updated.online_eligible is False
    await database.dispose()


def test_fixed_replay_registry_has_exact_categories_and_complete_contracts() -> None:
    cases = fixed_replay_cases()

    assert len(cases) == 12
    assert len({case.case_id for case in cases}) == 12
    assert Counter(case.category for case in cases) == {
        ReplayCaseCategory.SIMPLE: 4,
        ReplayCaseCategory.MULTI_STEP: 4,
        ReplayCaseCategory.TOOL_FAILURE: 2,
        ReplayCaseCategory.SECURITY_REJECTION: 2,
    }
    for case in cases:
        view = case.to_dict()
        assert view["input"]
        assert view["fixture"]["fixture_id"]
        assert view["tool_policy"]["readonly_only"] is True
        assert "required_evidence" in view
        assert case.max_ticks >= 1
        assert case.max_tool_calls >= 1
        assert view["success_oracle"]["expected_task_status"]


def test_baseline_runs_real_runtime_and_records_oracle_results_only() -> None:
    run = SkillBaselineRunner(REPOSITORY).run(tenant_id="tenant-a")

    assert run.candidate_loaded is False
    assert len(run.results) == 12
    assert run.oracle_pass_count == 12
    assert all(item.oracle_passed for item in run.results)
    assert all(item.tick_count > 0 for item in run.results)
    assert all(item.tool_call_count > 0 for item in run.results)
    assert all(item.usage["total_tokens"] > 0 for item in run.results)
    view = run.to_dict()
    assert view["measurement"] == "runtime_observed"
    assert "success_rate" not in view
    safety = next(
        item for item in run.results if item.case_id == "safety-unknown-write-tool"
    )
    assert safety.task_status == "failed"
    assert safety.rejected_tool_calls[0]["reason_code"] == "UNKNOWN_TOOL"
    assert safety.successful_tool_calls == ()


@pytest.mark.asyncio
async def test_baseline_result_persists_with_tenant_isolation() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = SkillEvaluationRepository(database.session_factory)
    run = SkillBaselineRunner(REPOSITORY).run(
        tenant_id="tenant-a", cases=fixed_replay_cases()[:2]
    )

    persisted = await repository.save_baseline(run)

    assert persisted.run_id == run.run_id
    assert len(persisted.results) == 2
    assert await repository.get_baseline("tenant-b", run.run_id) is None
    loaded = await repository.get_baseline("tenant-a", run.run_id)
    assert loaded is not None
    assert loaded.case_definition_digest == run.case_definition_digest
    assert loaded.candidate_loaded is False
    await database.dispose()


def test_skill_evaluation_api_lists_cases_and_runs_selected_baseline() -> None:
    settings = AthenaSettings(
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:", auto_migrate=True)
    )
    with TestClient(create_app(settings=settings)) as client:
        cases_response = client.get("/api/skill-evaluation/cases")
        assert cases_response.status_code == 200
        assert cases_response.json()["data"]["case_count"] == 12

        run_response = client.post(
            "/api/skill-evaluation/baseline-runs",
            json={
                "case_ids": [
                    "simple-search-symbol",
                    "safety-path-escape",
                ]
            },
        )
        assert run_response.status_code == 201, run_response.text
        run = run_response.json()["data"]
        assert run["candidate_loaded"] is False
        assert run["measurement"] == "runtime_observed"
        assert run["case_count"] == 2
        assert run["oracle_pass_count"] == 2

        loaded = client.get(f"/api/skill-evaluation/baseline-runs/{run['run_id']}")
        assert loaded.status_code == 200
        assert loaded.json()["data"]["run_id"] == run["run_id"]


def test_validation_baseline_migration_upgrade_and_downgrade() -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_validation_baseline_migration", MIGRATION_PATH
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
    )
    metadata.create_all(engine)

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()

    inspector = inspect(engine)
    assert {
        "skill_candidate_validation_reports",
        "skill_baseline_runs",
    }.issubset(inspector.get_table_names())
    assert "schema_version" in {
        item["name"] for item in inspector.get_columns("skill_candidates")
    }

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
    inspector = inspect(engine)
    assert "skill_candidate_validation_reports" not in inspector.get_table_names()
    assert "skill_baseline_runs" not in inspector.get_table_names()
    assert "schema_version" not in {
        item["name"] for item in inspector.get_columns("skill_candidates")
    }
    engine.dispose()
