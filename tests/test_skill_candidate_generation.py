"""P0 Eligible trajectory to generated, validated Candidate acceptance tests."""

from __future__ import annotations

import asyncio
import importlib.util
from dataclasses import replace
from datetime import UTC, datetime
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
from athena.api.server import create_app
from athena.application.skill_candidate_generation_service import (
    SkillCandidateGenerationService,
)
from athena.application.skill_candidate_service import SkillCandidateService
from athena.config import AthenaSettings, DatabaseSettings
from athena.infra.llm import LLMResponse
from athena.learning.candidate_generation import (
    CandidateGenerationError,
    CandidateGenerationOutput,
    CandidateGenerationPayload,
    LLMCandidateGenerator,
    TrajectoryDigestBuilder,
)
from athena.runtime.learning import (
    TrajectoryAdmission,
    TrajectoryStatus,
    TrajectorySummary,
)

REPOSITORY = Path(__file__).parent / "fixtures" / "runtime_repo"
MIGRATION_PATH = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "20260812_0012_skill_candidate_generation.py"
)


def _eligible_summary(
    trajectory_id: str = "trajectory-generation-a",
    *,
    tenant_id: str = "tenant-a",
) -> TrajectorySummary:
    return TrajectorySummary(
        trajectory_id=trajectory_id,
        tenant_id=tenant_id,
        source_task_id=f"task-{trajectory_id}",
        schema_version="runtime.learning.trajectory.v1",
        status=TrajectoryStatus.ELIGIBLE,
        task_summary="Diagnose a repeatable pricing calculation failure.",
        outcome_summary={
            "root_cause": "The discount boundary uses the wrong comparison.",
            "repair_recommendation": "Correct the comparison and retain test Evidence.",
        },
        tool_calls=(
            {
                "sequence": 1,
                "tool_name": "search_code",
                "status": "succeeded",
                "reason_code": None,
                "evidence_id": "evidence-search",
            },
            {
                "sequence": 2,
                "tool_name": "read_file_range",
                "status": "succeeded",
                "reason_code": None,
                "evidence_id": "evidence-read",
            },
            {
                "sequence": 3,
                "tool_name": "run_test",
                "status": "succeeded",
                "reason_code": None,
                "evidence_id": "evidence-test",
            },
        ),
        evidence=(
            {
                "evidence_id": "evidence-search",
                "source": "tool:search_code",
                "summary": "The pricing entry point was located.",
            },
            {
                "evidence_id": "evidence-read",
                "source": "tool:read_file_range",
                "summary": "The bounded comparison was inspected.",
            },
            {
                "evidence_id": "evidence-test",
                "source": "tool:run_test",
                "summary": "The allowlisted check reproduced the failure.",
            },
        ),
        usage={"input_tokens": 300, "output_tokens": 100, "total_tokens": 400},
        budget={"total_tokens": 8_000, "consumed_tokens": 400, "within_budget": True},
        admission=TrajectoryAdmission(
            eligible=True,
            rejection_reasons=(),
            quality_score=1.0,
            quality_factors={
                "task_success": 1.0,
                "evidence_completeness": 1.0,
                "tool_efficiency": 1.0,
                "reusability": 1.0,
                "safety_stability": 1.0,
            },
            quality_explanations=("all fixed admission checks passed",),
            checks={
                "task_succeeded": True,
                "evidence_complete": True,
                "security_clear": True,
                "tool_authorized": True,
                "within_budget": True,
            },
        ),
        redaction_count=2,
        created_at=datetime.now(UTC),
    )


def _payload(
    *,
    skill_id: str = "pricing.diagnosis.generated",
    description: str = "Diagnose repeatable pricing failures from bounded Evidence.",
) -> CandidateGenerationPayload:
    return CandidateGenerationPayload.model_validate(
        {
            "skill_id": skill_id,
            "name": "generated-pricing-diagnosis",
            "version": 1,
            "description": description,
            "trigger": {
                "task_type": "repository_diagnosis",
                "keywords": ["pricing", "calculation"],
            },
            "allowed_tools": ["search_code", "read_file_range", "run_test"],
            "procedure": [
                "Search for the pricing calculation entry point.",
                "Read bounded source and retain linked Evidence.",
                "Run the allowlisted repository check.",
            ],
            "failure_recovery": [
                "Stop after any rejected tool call and request human review."
            ],
            "success_contract": {
                "requires_root_cause": True,
                "requires_evidence": True,
            },
            "evidence_requirements": [
                "Source-linked Evidence must directly support the root cause."
            ],
            "token_budget_hint": 8_000,
            "risk_level": "S1",
        }
    )


class FakeGenerator:
    def __init__(
        self,
        payloads: tuple[CandidateGenerationPayload, ...] | None = None,
        *,
        error_code: str | None = None,
    ) -> None:
        self.payloads = payloads or (_payload(),)
        self.error_code = error_code
        self.calls = 0
        self.digests = []

    async def generate(self, digest):
        self.calls += 1
        self.digests.append(digest)
        if self.error_code:
            raise CandidateGenerationError(self.error_code)
        payload = self.payloads[min(self.calls - 1, len(self.payloads) - 1)]
        return CandidateGenerationOutput(
            payload=payload,
            generator="fake-candidate-generator.v1",
            model="fake-model-v1",
            usage={"input_tokens": 37, "output_tokens": 19, "total_tokens": 56},
        )


async def _service(fake: FakeGenerator):
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = SkillCandidateRepository(database.session_factory)
    candidate_service = SkillCandidateService(repository)
    service = SkillCandidateGenerationService(repository, candidate_service, fake)
    return database, repository, service


def test_digest_contains_only_bounded_redacted_summary_content() -> None:
    summary = replace(
        _eligible_summary(),
        task_summary=(
            "Diagnose pricing with api_key=top-secret-value and "
            "hidden_thought=private reasoning"
        ),
    )
    digest = TrajectoryDigestBuilder().build((summary,))

    prompt = digest.to_prompt_dict()
    rendered = str(prompt)
    assert summary.tenant_id not in rendered
    assert summary.trajectory_id not in rendered
    assert "evidence-search" not in rendered
    assert "top-secret-value" not in rendered
    assert "private reasoning" not in rendered
    assert "[REDACTED_SECRET]" in rendered
    assert "[REDACTED_REASONING]" in rendered
    assert prompt["raw_artifacts_included"] is False
    assert prompt["hidden_reasoning_included"] is False
    assert digest.available_tools == ("search_code", "read_file_range", "run_test")


@pytest.mark.asyncio
async def test_generation_persists_candidate_validation_usage_and_audit() -> None:
    fake = FakeGenerator()
    database, repository, service = await _service(fake)
    summary = _eligible_summary()
    await repository.save_trajectory(summary)

    run = await service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(summary.trajectory_id,),
        created_by="operator-a",
    )

    assert run.status == "succeeded"
    assert run.candidate_id is not None
    assert run.validation_report_id is not None
    assert run.model == "fake-model-v1"
    assert run.usage == {"input_tokens": 37, "output_tokens": 19, "total_tokens": 56}
    assert run.latency_ms is not None and run.latency_ms >= 0
    assert run.to_dict()["raw_response_persisted"] is False
    assert "response" not in run.digest
    candidate = await repository.get("tenant-a", run.candidate_id)
    assert candidate is not None
    assert candidate.skill_id == "pricing.diagnosis.generated"
    assert candidate.status == "candidate"
    assert candidate.evaluation_status == "validation_passed"
    assert candidate.online_eligible is False
    assert candidate.manifest["activation_allowed"] is False
    report = await repository.get_validation("tenant-a", run.validation_report_id)
    assert report is not None and report.passed is True
    persisted = await service.get("tenant-a", run.run_id)
    assert persisted == run
    assert await service.get("tenant-b", run.run_id) is None
    await database.dispose()


@pytest.mark.asyncio
async def test_repeated_source_is_idempotent_and_does_not_call_model_twice() -> None:
    fake = FakeGenerator()
    database, repository, service = await _service(fake)
    summary = _eligible_summary()
    await repository.save_trajectory(summary)

    first = await service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(summary.trajectory_id,),
        created_by="operator-a",
    )
    second = await service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(summary.trajectory_id,),
        created_by="operator-a",
    )

    assert second.run_id == first.run_id
    assert second.candidate_id == first.candidate_id
    assert fake.calls == 1
    assert len(await repository.list_deduplication_candidates("tenant-a")) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_rule_duplicate_records_explanation_without_second_candidate() -> None:
    second_payload = _payload(skill_id="pricing.diagnosis.generated.v2")
    fake = FakeGenerator((_payload(), second_payload))
    database, repository, service = await _service(fake)
    first_summary = _eligible_summary()
    second_summary = replace(
        first_summary,
        trajectory_id="trajectory-generation-b",
        source_task_id="task-generation-b",
    )
    await repository.save_trajectory(first_summary)
    await repository.save_trajectory(second_summary)
    first = await service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(first_summary.trajectory_id,),
        created_by="operator-a",
    )
    second = await service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(second_summary.trajectory_id,),
        created_by="operator-a",
    )

    assert first.status == "succeeded"
    assert second.status == "duplicate"
    assert second.duplicate_of_candidate_id == first.candidate_id
    assert second.deduplication["kind"] == "semantic_rule"
    assert second.deduplication["canonical_trigger_equal"] is True
    assert second.deduplication["procedure_similarity"] >= 0.82
    assert fake.calls == 2
    assert len(await repository.list_deduplication_candidates("tenant-a")) == 1
    await database.dispose()


@pytest.mark.asyncio
async def test_ineligible_timeout_and_unsafe_outputs_fail_closed() -> None:
    ineligible_fake = FakeGenerator()
    database, repository, service = await _service(ineligible_fake)
    eligible = _eligible_summary()
    rejected = replace(
        eligible,
        trajectory_id="trajectory-rejected",
        source_task_id="task-rejected",
        status=TrajectoryStatus.REJECTED,
        admission=replace(
            eligible.admission,
            eligible=False,
            rejection_reasons=("SECURITY_VIOLATION",),
        ),
    )
    await repository.save_trajectory(rejected)
    rejected_run = await service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(rejected.trajectory_id,),
        created_by="operator-a",
    )
    assert rejected_run.status == "failed"
    assert rejected_run.failure_code == "CANDIDATE_GENERATION_SOURCE_NOT_ELIGIBLE"
    assert ineligible_fake.calls == 0

    timeout_fake = FakeGenerator(error_code="CANDIDATE_GENERATION_TIMEOUT")
    timeout_service = SkillCandidateGenerationService(
        repository, SkillCandidateService(repository), timeout_fake
    )
    timeout_summary = replace(
        eligible,
        trajectory_id="trajectory-timeout",
        source_task_id="task-timeout",
    )
    await repository.save_trajectory(timeout_summary)
    timeout_run = await timeout_service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(timeout_summary.trajectory_id,),
        created_by="operator-a",
    )
    assert timeout_run.status == "failed"
    assert timeout_run.failure_code == "CANDIDATE_GENERATION_TIMEOUT"
    assert timeout_run.usage == {}

    unsafe_fake = FakeGenerator(
        (_payload(description="authorization: Bearer super-secret-value"),)
    )
    unsafe_service = SkillCandidateGenerationService(
        repository, SkillCandidateService(repository), unsafe_fake
    )
    unsafe_summary = replace(
        eligible,
        trajectory_id="trajectory-unsafe",
        source_task_id="task-unsafe",
    )
    await repository.save_trajectory(unsafe_summary)
    unsafe_run = await unsafe_service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(unsafe_summary.trajectory_id,),
        created_by="operator-a",
    )
    assert unsafe_run.status == "rejected"
    assert unsafe_run.failure_code == "CANDIDATE_GENERATION_OUTPUT_UNSAFE"
    assert unsafe_run.candidate_id is None
    assert "super-secret-value" not in str(unsafe_run.to_dict())
    await database.dispose()


@pytest.mark.asyncio
async def test_existing_validator_rejects_generated_invisible_control_text() -> None:
    fake = FakeGenerator((_payload(description="diagnosis\u202eunsafe"),))
    database, repository, service = await _service(fake)
    summary = _eligible_summary()
    await repository.save_trajectory(summary)

    run = await service.generate(
        tenant_id="tenant-a",
        source_trajectory_ids=(summary.trajectory_id,),
        created_by="operator-a",
    )

    assert run.status == "rejected"
    assert run.failure_code == "CANDIDATE_VALIDATION_FAILED"
    assert run.candidate_id is not None
    candidate = await repository.get("tenant-a", run.candidate_id)
    assert candidate is not None
    assert candidate.status == "rejected"
    assert candidate.evaluation_status == "validation_failed"
    assert candidate.online_eligible is False
    await database.dispose()


@pytest.mark.asyncio
async def test_llm_adapter_rejects_malformed_json_and_enforces_timeout() -> None:
    class MalformedClient:
        async def complete(self, messages):
            return LLMResponse(
                content="raw secret response that is not JSON",
                model="malformed-model",
                usage={"prompt_tokens": 5, "completion_tokens": 2},
            )

    class SlowClient:
        async def complete(self, messages):
            await asyncio.sleep(0.05)
            return LLMResponse(content="{}", model="slow-model")

    digest = TrajectoryDigestBuilder().build((_eligible_summary(),))
    with pytest.raises(CandidateGenerationError) as malformed:
        await LLMCandidateGenerator(MalformedClient()).generate(digest)
    assert malformed.value.error_code == "CANDIDATE_GENERATION_OUTPUT_INVALID"
    assert malformed.value.model == "malformed-model"
    assert malformed.value.usage["total_tokens"] == 7
    assert "raw secret" not in str(malformed.value)

    with pytest.raises(CandidateGenerationError) as timeout:
        await LLMCandidateGenerator(SlowClient(), timeout_seconds=0.001).generate(
            digest
        )
    assert timeout.value.error_code == "CANDIDATE_GENERATION_TIMEOUT"


def test_candidate_generation_api_is_explicit_and_queryable() -> None:
    settings = AthenaSettings(
        database=DatabaseSettings(url="sqlite+aiosqlite:///:memory:", auto_migrate=True)
    )
    app = create_app(settings=settings)
    fake = FakeGenerator()
    assert app.state.skill_candidate_repository is not None
    assert app.state.skill_candidate_service is not None
    app.state.skill_candidate_generation_service = SkillCandidateGenerationService(
        app.state.skill_candidate_repository,
        app.state.skill_candidate_service,
        fake,
    )

    with TestClient(app) as client:
        task = client.post(
            "/api/runtime/tasks",
            json={
                "goal": "Diagnose the pricing calculation failure",
                "repository_path": str(REPOSITORY),
                "profile": "standard",
            },
        ).json()["data"]
        client.post(f"/api/runtime/tasks/{task['id']}/run")
        trajectory_response = client.post(
            f"/api/runtime/skills/tasks/{task['id']}/trajectory"
        )
        assert trajectory_response.status_code == 200, trajectory_response.text
        trajectory = trajectory_response.json()["data"]
        assert fake.calls == 0

        generated_response = client.post(
            "/api/skill-candidates/generations",
            json={"source_trajectory_ids": [trajectory["trajectory_id"]]},
        )
        assert generated_response.status_code == 201, generated_response.text
        generated = generated_response.json()["data"]
        assert generated["status"] == "succeeded"
        assert generated["activation_allowed"] is False
        assert generated["raw_response_persisted"] is False
        assert fake.calls == 1

        loaded = client.get(f"/api/skill-candidates/generations/{generated['run_id']}")
        assert loaded.status_code == 200
        assert loaded.json()["data"]["candidate_id"] == generated["candidate_id"]


def test_candidate_generation_migration_upgrade_and_downgrade() -> None:
    spec = importlib.util.spec_from_file_location(
        "skill_candidate_generation_migration", MIGRATION_PATH
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
    assert "skill_candidate_generation_runs" in inspector.get_table_names()
    columns = {
        item["name"]
        for item in inspector.get_columns("skill_candidate_generation_runs")
    }
    assert {
        "digest_json",
        "candidate_id",
        "validation_report_id",
        "usage_json",
        "latency_ms",
        "failure_code",
    }.issubset(columns)
    unique_constraints = inspector.get_unique_constraints(
        "skill_candidate_generation_runs"
    )
    assert any(
        item["name"] == "uq_skill_candidate_generation_source"
        for item in unique_constraints
    )

    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.downgrade()
    assert "skill_candidate_generation_runs" not in inspect(engine).get_table_names()
    engine.dispose()
