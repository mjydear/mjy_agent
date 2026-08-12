"""Durable idempotency tests for external Tool effects."""

from __future__ import annotations

import pytest

from athena.api.repositories import (
    Database,
    ToolEffectConflictError,
    ToolEffectRepository,
)
from athena.config import DatabaseSettings


@pytest.mark.asyncio
async def test_tool_effect_call_id_is_idempotent_and_records_post_condition() -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    effects = ToolEffectRepository(database.session_factory)

    started, replayed = await effects.start(
        tenant_id="tenant-a",
        task_id="ops-1",
        call_id="call-1",
        tool_name="k8s.deployment.restart",
        arguments={"namespace": "payment", "name": "api"},
        plan_hash="plan-1",
    )
    assert replayed is False
    replay, replayed = await effects.start(
        tenant_id="tenant-a",
        task_id="ops-1",
        call_id="call-1",
        tool_name="k8s.deployment.restart",
        arguments={"name": "api", "namespace": "payment"},
        plan_hash="plan-1",
    )
    assert replayed is True
    assert replay.effect_id == started.effect_id
    with pytest.raises(ToolEffectConflictError):
        await effects.start(
            tenant_id="tenant-a",
            task_id="ops-1",
            call_id="call-1",
            tool_name="k8s.deployment.restart",
            arguments={"namespace": "payment", "name": "different"},
            plan_hash="plan-1",
        )
    finished = await effects.finish(
        tenant_id="tenant-a",
        task_id="ops-1",
        call_id="call-1",
        result={"status": "restarted"},
        post_condition={"ready_replicas": 2},
    )
    assert finished.status == "succeeded"
    assert finished.post_condition == {"ready_replicas": 2}
    await database.dispose()
