from __future__ import annotations

import pytest

from athena.api.repositories import Database, TaskCreate, TaskRepository
from athena.application.durable_alert_service import _workflow_type_for_alert
from athena.config import DatabaseSettings


@pytest.mark.parametrize(
    ("alert_name", "workflow"),
    [
        ("KubePodCrashLooping", "crashloop"),
        ("KubePodPending", "pod_pending"),
        ("KubePodImagePullBackOff", "image_pull"),
        ("KubePodFailedScheduling", "resource_pressure"),
    ],
)
def test_alert_workflow_mapping_is_explicit_and_bounded(
    alert_name: str, workflow: str
) -> None:
    assert _workflow_type_for_alert(alert_name) == workflow


def test_unknown_alert_does_not_default_to_crashloop() -> None:
    assert _workflow_type_for_alert("SomeUnrelatedAlert") == "unsupported"


@pytest.mark.asyncio
async def test_persisted_task_keeps_explicit_alert_workflow_for_worker_routing() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    try:
        tasks = TaskRepository(database.session_factory)
        created, replayed = await tasks.create_task(
            TaskCreate(
                task_id="ops-image-pull",
                tenant_id="tenant-a",
                objective="diagnose KubePodImagePullBackOff",
                environment_id="default",
                environment_mode="mock",
                scope={"namespace": "payment", "alert_name": "KubePodImagePullBackOff"},
                policy_snapshot={"readonly": True},
                config_snapshot={},
                budget={"remaining_steps": 4, "remaining_tokens": 6000},
                execution_profile="bounded_policy_loop",
                workflow_type="image_pull",
            ),
            idempotency_key="alert-image-pull-1",
            request_hash="alert-image-pull-request",
        )

        persisted = await tasks.get_task("tenant-a", created.task_id)

        assert replayed is False
        assert persisted is not None
        assert persisted.workflow_type == "image_pull"
    finally:
        await database.dispose()
