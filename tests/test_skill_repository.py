"""P5-02 governed Skill repository lifecycle tests."""

from __future__ import annotations

import pytest

from athena.api.repositories import Database
from athena.api.repositories.skill_repository import (
    ACTIVE_STATUS,
    ARCHIVED_STATUS,
    DRAFT_STATUS,
    REJECTED_STATUS,
    REVIEW_PENDING_STATUS,
    SkillLifecycleError,
    SkillRepository,
)
from athena.config import DatabaseSettings


def _manifest(name: str = "pending-pod-triage") -> dict[str, object]:
    return {
        "name": name,
        "capabilities": ["k8s.workload.read", "k8s.events.read"],
        "risk_level": "S1",
        "summary": "Diagnose Pending pods using workload and event evidence.",
    }


def _procedure(step: str = "collect pending pod events") -> dict[str, object]:
    return {
        "steps": [
            "list pods in scoped namespace",
            "describe selected Pending pod",
            step,
        ],
        "validation": "root cause must cite event evidence",
    }


@pytest.mark.asyncio
async def test_skill_lifecycle_filters_draft_and_activates_reviewed_version() -> None:
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
        source_task_id="ops-1",
        benchmark_report_id="bench-1",
    )

    assert draft.status == DRAFT_STATUS
    assert (
        await repo.list_active_for_capabilities(
            "tenant-a",
            environment_type="kubernetes",
            capabilities=frozenset({"k8s.workload.read", "k8s.events.read"}),
        )
        == ()
    )
    pending = await repo.submit_review("tenant-a", draft.version_id)
    assert pending is not None
    assert pending.status == REVIEW_PENDING_STATUS
    active = await repo.approve(
        "tenant-a", draft.version_id, reviewed_by="lead-sre", note="safe"
    )
    assert active is not None
    assert active.status == ACTIVE_STATUS
    assert active.reviewed_by == "lead-sre"
    assert len(active.checksum) == 64
    assert await repo.get_active("tenant-a", definition.skill_id) == active
    assert await repo.get_active("tenant-b", definition.skill_id) is None

    recalled = await repo.list_active_for_capabilities(
        "tenant-a",
        environment_type="kubernetes",
        capabilities=frozenset({"k8s.workload.read", "k8s.events.read"}),
    )
    assert recalled == (active,)
    assert (
        await repo.list_active_for_capabilities(
            "tenant-a",
            environment_type="kubernetes",
            capabilities=frozenset({"k8s.workload.read"}),
        )
        == ()
    )
    await database.dispose()


@pytest.mark.asyncio
async def test_skill_new_version_archives_previous_and_can_rollback() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repo = SkillRepository(database.session_factory)

    definition, v1 = await repo.create_draft(
        "tenant-a",
        name="pending-pod-triage",
        owner="sre-a",
        environment_type="kubernetes",
        capabilities=("k8s.workload.read", "k8s.events.read"),
        manifest=_manifest(),
        procedure=_procedure(),
        created_by="curator",
    )
    await repo.submit_review("tenant-a", v1.version_id)
    active_v1 = await repo.approve("tenant-a", v1.version_id, reviewed_by="lead")
    assert active_v1 is not None

    _, v2 = await repo.create_draft(
        "tenant-a",
        name="pending-pod-triage",
        owner="sre-a",
        environment_type="kubernetes",
        capabilities=("k8s.workload.read", "k8s.events.read"),
        manifest=_manifest(),
        procedure=_procedure("collect scheduling and image pull events"),
        created_by="curator",
    )
    await repo.submit_review("tenant-a", v2.version_id)
    active_v2 = await repo.approve("tenant-a", v2.version_id, reviewed_by="lead")
    assert active_v2 is not None
    assert active_v2.version == 2
    assert (await repo.get_active("tenant-a", definition.skill_id)) == active_v2

    recalled = await repo.list_active_for_capabilities(
        "tenant-a",
        environment_type="kubernetes",
        capabilities=frozenset({"k8s.workload.read", "k8s.events.read"}),
    )
    assert recalled == (active_v2,)

    rolled_back = await repo.rollback(
        "tenant-a",
        skill_id=definition.skill_id,
        target_version_id=active_v1.version_id,
        reviewed_by="lead",
        note="regression",
    )
    assert rolled_back is not None
    assert rolled_back.status == ACTIVE_STATUS
    assert rolled_back.version_id == active_v1.version_id
    assert (await repo.get_active("tenant-a", definition.skill_id)) == rolled_back

    recalled_after_rollback = await repo.list_active_for_capabilities(
        "tenant-a",
        environment_type="kubernetes",
        capabilities=frozenset({"k8s.workload.read", "k8s.events.read"}),
    )
    assert recalled_after_rollback == (rolled_back,)
    assert active_v2.status != ARCHIVED_STATUS  # immutable returned snapshot
    await database.dispose()


@pytest.mark.asyncio
async def test_skill_review_rejection_and_state_guards() -> None:
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

    with pytest.raises(SkillLifecycleError) as approve_error:
        await repo.approve("tenant-a", draft.version_id, reviewed_by="lead")
    assert approve_error.value.error_code == "SKILL_VERSION_NOT_PENDING_REVIEW"

    pending = await repo.submit_review("tenant-a", draft.version_id)
    assert pending is not None
    rejected = await repo.reject(
        "tenant-a", draft.version_id, reviewed_by="lead", note="too narrow"
    )
    assert rejected is not None
    assert rejected.status == REJECTED_STATUS
    with pytest.raises(SkillLifecycleError) as resubmit_error:
        await repo.submit_review("tenant-a", draft.version_id)
    assert resubmit_error.value.error_code == "SKILL_VERSION_NOT_REVIEWABLE"
    await database.dispose()


@pytest.mark.asyncio
async def test_skill_manifest_rejects_write_capability_and_scripts() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repo = SkillRepository(database.session_factory)

    with pytest.raises(ValueError, match="readonly"):
        await repo.create_draft(
            "tenant-a",
            name="dangerous",
            owner="sre-a",
            environment_type="kubernetes",
            capabilities=("k8s.workload.write",),
            manifest={
                "name": "dangerous",
                "capabilities": ["k8s.workload.write"],
            },
            procedure=_procedure(),
            created_by="curator",
        )
    with pytest.raises(ValueError, match="scripts"):
        await repo.create_draft(
            "tenant-a",
            name="scripted",
            owner="sre-a",
            environment_type="kubernetes",
            capabilities=("k8s.workload.read",),
            manifest={
                "name": "scripted",
                "capabilities": ["k8s.workload.read"],
                "script": "kubectl delete pod",
            },
            procedure=_procedure(),
            created_by="curator",
        )
    await database.dispose()
