"""P5-03 Skill replay evaluation, review, activation and rollback tests."""

from __future__ import annotations

import pytest

from athena.api.repositories import Database
from athena.api.repositories.skill_repository import (
    ACTIVE_STATUS,
    EVALUATING_STATUS,
    REVIEW_PENDING_STATUS,
    SkillLifecycleError,
    SkillRepository,
)
from athena.application.skill_replay import SkillReplayCase, SkillReplayEvaluator
from athena.config import DatabaseSettings


def _manifest() -> dict[str, object]:
    return {
        "name": "pending-pod-triage",
        "capabilities": ["k8s.workload.read", "k8s.events.read"],
        "risk_level": "S1",
    }


def _procedure(*, with_events: bool = True) -> dict[str, object]:
    steps = ["list pods", "describe pending pod"]
    if with_events:
        steps.append("collect event evidence")
    return {"steps": steps, "validation": "root cause must match replay oracle"}


def _cases() -> tuple[SkillReplayCase, ...]:
    return (
        SkillReplayCase(
            case_id="pod-pending-scheduler",
            workflow_type="pod_pending",
            required_capabilities=frozenset({"k8s.workload.read", "k8s.events.read"}),
            event_reasons=("FailedScheduling",),
            expected_root_cause="Scheduler could not place the pod",
        ),
        SkillReplayCase(
            case_id="pod-pending-imagepull",
            workflow_type="pod_pending",
            required_capabilities=frozenset({"k8s.workload.read", "k8s.events.read"}),
            event_reasons=("ErrImagePull", "ImagePullBackOff"),
            expected_root_cause="Image pull failure prevented the pod from starting",
        ),
    )


@pytest.mark.asyncio
async def test_replay_pass_promotes_skill_to_review_then_activation() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repo = SkillRepository(database.session_factory)
    definition, draft = await repo.create_draft(
        "tenant-a",
        name="pending-pod-triage",
        owner="sre-a",
        environment_type="kubernetes",
        capabilities=("k8s.workload.read", "k8s.events.read"),
        manifest=_manifest(),
        procedure=_procedure(),
        created_by="curator",
    )
    report = SkillReplayEvaluator().evaluate(draft, _cases())
    assert report.passed is True
    assert report.pass_rate == 1.0
    assert {result.reason_code for result in report.results} == {"REPLAY_PASSED"}

    evaluated = await repo.record_evaluation(
        "tenant-a",
        draft.version_id,
        report_id=report.report_id,
        passed=report.passed,
    )
    assert evaluated is not None
    assert evaluated.status == REVIEW_PENDING_STATUS
    assert evaluated.benchmark_report_id == report.report_id

    active = await repo.approve("tenant-a", draft.version_id, reviewed_by="lead")
    assert active is not None
    assert active.status == ACTIVE_STATUS
    assert await repo.get_active("tenant-a", definition.skill_id) == active
    await database.dispose()


@pytest.mark.asyncio
async def test_replay_failure_keeps_skill_out_of_review_and_active_recall() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repo = SkillRepository(database.session_factory)
    _, draft = await repo.create_draft(
        "tenant-a",
        name="pending-pod-triage",
        owner="sre-a",
        environment_type="kubernetes",
        capabilities=("k8s.workload.read", "k8s.events.read"),
        manifest=_manifest(),
        procedure=_procedure(with_events=False),
        created_by="curator",
    )
    report = SkillReplayEvaluator().evaluate(draft, _cases())
    assert report.passed is False
    assert {result.reason_code for result in report.results} == {
        "REPLAY_PROCEDURE_MISSING_EVENT_EVIDENCE"
    }

    evaluated = await repo.record_evaluation(
        "tenant-a",
        draft.version_id,
        report_id=report.report_id,
        passed=report.passed,
    )
    assert evaluated is not None
    assert evaluated.status == EVALUATING_STATUS

    with pytest.raises(SkillLifecycleError) as exc:
        await repo.approve("tenant-a", draft.version_id, reviewed_by="lead")
    assert exc.value.error_code == "SKILL_VERSION_NOT_PENDING_REVIEW"
    assert (
        await repo.list_active_for_capabilities(
            "tenant-a",
            environment_type="kubernetes",
            capabilities=frozenset({"k8s.workload.read", "k8s.events.read"}),
        )
        == ()
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_replay_report_id_is_stable_for_same_version_and_cases() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repo = SkillRepository(database.session_factory)
    _, draft = await repo.create_draft(
        "tenant-a",
        name="pending-pod-triage",
        owner="sre-a",
        environment_type="kubernetes",
        capabilities=("k8s.workload.read", "k8s.events.read"),
        manifest=_manifest(),
        procedure=_procedure(),
        created_by="curator",
    )
    evaluator = SkillReplayEvaluator()
    first = evaluator.evaluate(draft, _cases())
    second = evaluator.evaluate(draft, _cases())
    assert first.report_id == second.report_id
    await database.dispose()
