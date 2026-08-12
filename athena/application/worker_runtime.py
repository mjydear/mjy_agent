"""Process runtime that relays durable Outbox messages and executes Agent tasks."""

from __future__ import annotations

import asyncio
import logging
import socket
import uuid

from athena.api.repositories import (
    Database,
    DiagnosisOutcomeRepository,
    EvidenceRepository,
    OutboxRepository,
    TaskRepository,
)
from athena.application.diagnosis_handler_registry import DiagnosisHandlerRegistry
from athena.application.diagnosis_outcome_service import DiagnosisOutcomeService
from athena.application.durable_outcome_handler import DurableOutcomeRecordingHandler
from athena.application.durable_worker import DurableTaskWorker
from athena.application.outbox_relay import OutboxRelay
from athena.config import AthenaSettings
from athena.infra.task_stream import RedisTaskStream
from athena.infra.evidence_content import LocalEvidenceContentStore

logger = logging.getLogger(__name__)


def build_diagnosis_handler_registry(
    settings: AthenaSettings, evidence: EvidenceRepository | None = None
) -> DiagnosisHandlerRegistry:
    """Construct the worker's explicit readonly diagnosis handler registry."""
    return DiagnosisHandlerRegistry.from_settings(settings, evidence)


async def run_worker(settings: AthenaSettings, *, once: bool = False) -> None:
    if not settings.database.url:
        raise RuntimeError("ATHENA_DATABASE_URL is required for athena worker")
    if not settings.cache.redis_url:
        raise RuntimeError("ATHENA_REDIS_URL is required for athena worker")

    database = Database(settings.database)
    stream = RedisTaskStream(
        settings.cache.redis_url,
        stream_name=settings.queue.stream_name,
        consumer_group=settings.queue.consumer_group,
    )
    worker_id = f"{socket.gethostname()}-{uuid.uuid4().hex[:12]}"
    tasks = TaskRepository(database.session_factory)
    evidence = EvidenceRepository(
        database.session_factory,
        LocalEvidenceContentStore(
            settings.evidence.local_root,
            max_content_bytes=settings.evidence.max_content_bytes,
        ),
    )
    diagnosis_outcomes = DiagnosisOutcomeRepository(database.session_factory)
    outcome_service = DiagnosisOutcomeService(diagnosis_outcomes)
    relay = OutboxRelay(
        OutboxRepository(database.session_factory),
        stream,
        owner=f"relay-{worker_id}",
        max_attempts=settings.queue.max_attempts,
    )
    worker = DurableTaskWorker(
        tasks,
        stream,
        DurableOutcomeRecordingHandler(
            build_diagnosis_handler_registry(settings, evidence), outcome_service
        ),
        worker_id=worker_id,
        lease_ttl_seconds=settings.worker.lease_ttl_seconds,
        max_attempts=settings.queue.max_attempts,
    )
    try:
        while True:
            published = await relay.dispatch_once(settings.worker.batch_size)
            processed = await worker.run_once(
                count=settings.worker.batch_size,
                block_ms=settings.queue.block_ms,
                reclaim_idle_ms=settings.queue.reclaim_idle_seconds * 1000,
            )
            if once:
                return
            if not published and not processed:
                await asyncio.sleep(settings.worker.poll_interval_seconds)
    finally:
        await stream.close()
        await database.dispose()
        logger.info("durable worker stopped id=%s", worker_id)
