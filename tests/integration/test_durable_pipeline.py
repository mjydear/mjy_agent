"""Optional PostgreSQL + Redis Streams integration test for the durable task path."""

from __future__ import annotations

import os
import uuid

import pytest

from athena.api.repositories import Database, OutboxRepository, TaskCreate, TaskRepository
from athena.application.outbox_relay import OutboxRelay
from athena.config import DatabaseSettings
from athena.infra.task_stream import RedisTaskStream


DATABASE_URL = os.getenv("ATHENA_TEST_DATABASE_URL")
REDIS_URL = os.getenv("ATHENA_TEST_REDIS_URL")

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_postgres_outbox_publishes_task_reference_to_redis_stream() -> None:
    if not DATABASE_URL or not REDIS_URL:
        pytest.skip("ATHENA_TEST_DATABASE_URL and ATHENA_TEST_REDIS_URL are required")

    suffix = uuid.uuid4().hex
    database = Database(DatabaseSettings(url=DATABASE_URL))
    tasks = TaskRepository(database.session_factory)
    stream_name = f"athena:test:tasks:{suffix}"
    group = f"athena-test-{suffix}"
    stream = RedisTaskStream(
        REDIS_URL, stream_name=stream_name, consumer_group=group
    )
    try:
        task_id = f"ops-integration-{suffix}"
        await tasks.create_task(
            TaskCreate(
                task_id=task_id,
                tenant_id="tenant-integration",
                objective="integration durable task",
                environment_id="env-integration",
                environment_mode="mock",
                scope={"namespace": "default"},
                policy_snapshot={"readonly": True},
                config_snapshot={"tool_set": "test"},
                budget={"remaining_steps": 1},
                execution_profile="bounded_policy_loop",
            )
        )
        relay = OutboxRelay(
            OutboxRepository(database.session_factory), stream, owner=f"relay-{suffix}"
        )
        assert await relay.dispatch_once() >= 1
        messages = await stream.consume(f"consumer-{suffix}", count=10, block_ms=1000)
        message = next(item for item in messages if item.task_id == task_id)
        assert message.tenant_id == "tenant-integration"
        await stream.ack(message.message_id)
    finally:
        await stream.close()
        await database.dispose()
