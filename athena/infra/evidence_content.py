"""Content-addressed local Evidence store with retention and shard routing helpers."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class EvidenceRetentionPolicy:
    """Retention policy attached to evidence metadata, not raw content."""

    retain_for_days: int
    legal_hold: bool = False

    def expires_at(self, collected_at: datetime) -> datetime | None:
        if self.legal_hold:
            return None
        if collected_at.tzinfo is None:
            raise ValueError("collected_at must be timezone-aware")
        return collected_at.astimezone(UTC) + timedelta(days=self.retain_for_days)

    def is_expired(
        self, collected_at: datetime, *, now: datetime | None = None
    ) -> bool:
        expiry = self.expires_at(collected_at)
        if expiry is None:
            return False
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return current.astimezone(UTC) >= expiry


@dataclass(frozen=True)
class EvidenceShardRoute:
    """Logical shard decision. Physical sharding stays behind this seam."""

    shard_id: str
    tenant_bucket: str
    task_bucket: str
    reason: str


class EvidenceShardRouter:
    """Deterministic shard router with an explicit benchmark gate.

    The default single-shard route is intentional: the architecture says physical
    shards are introduced only after a reproducible bottleneck. This class gives
    us the seam and tests without pretending a multi-shard deployment exists.
    """

    def __init__(
        self,
        *,
        shard_count: int = 1,
        physical_sharding_enabled: bool = False,
        benchmark_report_id: str | None = None,
    ) -> None:
        if shard_count < 1:
            raise ValueError("shard_count must be positive")
        if physical_sharding_enabled and not benchmark_report_id:
            raise ValueError("physical sharding requires a benchmark_report_id")
        self._shard_count = shard_count
        self._physical = physical_sharding_enabled
        self._benchmark_report_id = benchmark_report_id

    def route(self, tenant_id: str, task_id: str) -> EvidenceShardRoute:
        tenant_bucket = _bucket(tenant_id, length=16)
        task_bucket = _bucket(task_id, length=16)
        if not self._physical:
            return EvidenceShardRoute(
                shard_id="primary",
                tenant_bucket=tenant_bucket,
                task_bucket=task_bucket,
                reason="single_shard_until_benchmark_gate",
            )
        index = int(tenant_bucket[:8], 16) % self._shard_count
        return EvidenceShardRoute(
            shard_id=f"shard-{index:02d}",
            tenant_bucket=tenant_bucket,
            task_bucket=task_bucket,
            reason=f"benchmark:{self._benchmark_report_id}",
        )


def _bucket(value: str, *, length: int) -> str:
    if not value.strip():
        raise ValueError("bucket value must be non-empty")
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


class LocalEvidenceContentStore:
    def __init__(self, root: str | Path, *, max_content_bytes: int) -> None:
        self._root = Path(root)
        self._max_content_bytes = max_content_bytes

    async def put(
        self, tenant_id: str, task_id: str, content: object
    ) -> tuple[str, str]:
        encoded = json.dumps(
            content,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        if len(encoded) > self._max_content_bytes:
            raise ValueError("EVIDENCE_CONTENT_TOO_LARGE")
        digest = hashlib.sha256(encoded).hexdigest()
        tenant_bucket = _bucket(tenant_id, length=16)
        task_bucket = _bucket(task_id, length=16)
        relative = Path(tenant_bucket) / task_bucket / digest[:2] / f"{digest}.json"
        target = self._root / relative

        def write_once() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                return
            temporary = target.with_suffix(".tmp")
            temporary.write_bytes(encoded)
            temporary.replace(target)

        await asyncio.to_thread(write_once)
        return digest, f"local-evidence://{relative.as_posix()}"

    async def get(self, content_ref: str) -> object | None:
        prefix = "local-evidence://"
        if not content_ref.startswith(prefix):
            return None
        relative = Path(content_ref.removeprefix(prefix))
        if relative.is_absolute() or ".." in relative.parts:
            return None
        target = self._root / relative

        def read() -> object | None:
            if not target.exists():
                return None
            return json.loads(target.read_text(encoding="utf-8"))

        return await asyncio.to_thread(read)

    async def verify(self, content_ref: str, expected_hash: str) -> bool:
        target = self._resolve(content_ref)
        if target is None:
            return False

        def check() -> bool:
            if not target.exists():
                return False
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            return actual == expected_hash

        return await asyncio.to_thread(check)

    async def delete(self, content_ref: str) -> bool:
        target = self._resolve(content_ref)
        if target is None:
            return False

        def remove() -> bool:
            if not target.exists():
                return False
            target.unlink()
            return True

        return await asyncio.to_thread(remove)

    def _resolve(self, content_ref: str) -> Path | None:
        prefix = "local-evidence://"
        if not content_ref.startswith(prefix):
            return None
        relative = Path(content_ref.removeprefix(prefix))
        if relative.is_absolute() or ".." in relative.parts:
            return None
        return self._root / relative
