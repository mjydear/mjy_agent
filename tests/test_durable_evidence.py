"""Content-addressed Evidence persistence tests."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from athena.api.repositories import Database, EvidenceRepository
from athena.config import DatabaseSettings
from athena.infra.evidence_content import (
    EvidenceRetentionPolicy,
    EvidenceShardRouter,
    LocalEvidenceContentStore,
)


@pytest.mark.asyncio
async def test_evidence_keeps_metadata_in_database_and_content_by_hash(
    tmp_path,
) -> None:
    database = Database(DatabaseSettings(url="sqlite+aiosqlite:///:memory:"))
    await database.create_schema()
    repository = EvidenceRepository(
        database.session_factory,
        LocalEvidenceContentStore(tmp_path, max_content_bytes=1024),
    )
    evidence = await repository.create(
        tenant_id="tenant-a",
        task_id="ops-1",
        evidence_type="resource_snapshot",
        source="k8s.pod.list",
        data_origin="mock",
        summary="pod snapshot",
        content={"items": [{"name": "api-0"}]},
        observed_at=datetime.now(UTC),
    )
    assert evidence.content_ref.startswith("local-evidence://")
    assert len(evidence.content_hash) == 64
    assert await repository.get_content("tenant-a", evidence.evidence_id) == {
        "items": [{"name": "api-0"}]
    }
    assert await repository.get_content("tenant-b", evidence.evidence_id) is None
    listed = await repository.list_for_task("tenant-a", "ops-1")
    assert listed == (evidence,)
    assert await repository.verify_content_hash("tenant-a", evidence.evidence_id)
    await database.dispose()


@pytest.mark.asyncio
async def test_evidence_hash_verification_detects_tampering(tmp_path) -> None:
    store = LocalEvidenceContentStore(tmp_path, max_content_bytes=1024)
    digest, content_ref = await store.put("tenant-a", "task-1", {"safe": True})
    assert await store.verify(content_ref, digest)

    relative = content_ref.removeprefix("local-evidence://")
    (Path(tmp_path) / relative).write_text('{"safe":false}', encoding="utf-8")

    assert not await store.verify(content_ref, digest)


def test_evidence_retention_and_shard_router_are_explicitly_gated() -> None:
    collected_at = datetime(2026, 7, 1, tzinfo=UTC)
    policy = EvidenceRetentionPolicy(retain_for_days=7)
    assert policy.is_expired(collected_at, now=datetime(2026, 7, 8, tzinfo=UTC))
    assert not EvidenceRetentionPolicy(retain_for_days=1, legal_hold=True).is_expired(
        collected_at, now=datetime(2026, 8, 1, tzinfo=UTC)
    )

    router = EvidenceShardRouter()
    route = router.route("tenant-a", "task-1")
    assert route.shard_id == "primary"
    assert route.reason == "single_shard_until_benchmark_gate"

    with pytest.raises(ValueError):
        EvidenceShardRouter(shard_count=4, physical_sharding_enabled=True)

    measured = EvidenceShardRouter(
        shard_count=4,
        physical_sharding_enabled=True,
        benchmark_report_id="bench-20260719",
    ).route("tenant-a", "task-1")
    assert measured.shard_id.startswith("shard-")
    assert measured.reason == "benchmark:bench-20260719"
