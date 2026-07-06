"""审计哈希链存储：把关键操作以防篡改链的形式集中落库（Redis 可降级内存）。

设计要点：
- 每条记录带 seq(单调递增) 与 prev_hash，hash=sha256(prev_hash+规范化payload)。
- 任意历史记录被篡改都会导致后续 hash 校验失败，从而可检测。
- 存储走 CacheBackend（Redis 或内存降级），与会话/任务存储同一套基础设施。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from athena.infra.cache import CacheBackend

_GENESIS = "0" * 64  # 创世块 prev_hash


@dataclass(frozen=True)
class AuditRecord:
    """单条审计记录（含链式哈希字段）。"""

    seq: int
    timestamp: float
    actor: str
    tenant_id: str
    action: str
    resource: str
    success: bool
    detail: str
    prev_hash: str
    hash: str = ""

    def payload(self) -> dict[str, object]:
        """返回参与哈希计算的业务字段（不含 hash 自身）。"""
        data = asdict(self)
        data.pop("hash", None)
        return data


def _compute_hash(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class HashChainAuditStore:
    """基于缓存后端的防篡改审计链。

    键布局：
    - audit:seq          自增序号计数器
    - audit:head         最新记录的 {"seq","hash"} JSON
    - audit:{seq}        每条 AuditRecord JSON
    - audit:index        最近 seq 列表（JSON，截断到 max_index）
    """

    def __init__(self, cache: "CacheBackend", max_index: int = 1000) -> None:
        self._cache = cache
        self._max_index = max_index

    def append(
        self,
        *,
        actor: str,
        action: str,
        resource: str,
        success: bool,
        tenant_id: str = "system",
        detail: str = "",
        timestamp: float | None = None,
    ) -> AuditRecord:
        """追加一条审计记录并链接到上一条哈希。"""
        import time as _time

        seq = self._cache.incr("audit:seq")
        prev_hash = self._head_hash()
        record = AuditRecord(
            seq=seq,
            timestamp=timestamp if timestamp is not None else _time.time(),
            actor=actor,
            tenant_id=tenant_id,
            action=action,
            resource=resource,
            success=success,
            detail=detail,
            prev_hash=prev_hash,
        )
        digest = _compute_hash(record.payload())
        record = AuditRecord(**{**asdict(record), "hash": digest})
        self._cache.set(f"audit:{seq}", json.dumps(asdict(record), ensure_ascii=False))
        self._cache.set(
            "audit:head", json.dumps({"seq": seq, "hash": digest}, ensure_ascii=False)
        )
        self._push_index(seq)
        return record

    def _head_hash(self) -> str:
        raw = self._cache.get("audit:head")
        if not raw:
            return _GENESIS
        try:
            return str(json.loads(raw).get("hash", _GENESIS))
        except (ValueError, TypeError):
            return _GENESIS

    def _push_index(self, seq: int) -> None:
        raw = self._cache.get("audit:index")
        try:
            index = list(json.loads(raw)) if raw else []
        except (ValueError, TypeError):
            index = []
        index.append(seq)
        if len(index) > self._max_index:
            index = index[-self._max_index :]
        self._cache.set("audit:index", json.dumps(index, ensure_ascii=False))

    def _get(self, seq: int) -> AuditRecord | None:
        raw = self._cache.get(f"audit:{seq}")
        if not raw:
            return None
        try:
            return AuditRecord(**json.loads(raw))
        except (ValueError, TypeError):
            return None

    def head_seq(self) -> int:
        raw = self._cache.get("audit:head")
        if not raw:
            return 0
        try:
            return int(json.loads(raw).get("seq", 0))
        except (ValueError, TypeError):
            return 0

    def list(
        self, limit: int = 50, tenant_id: str | None = None
    ) -> list[AuditRecord]:
        """按时间倒序返回最近的审计记录，可按租户过滤。"""
        raw = self._cache.get("audit:index")
        try:
            index = list(json.loads(raw)) if raw else []
        except (ValueError, TypeError):
            index = []
        records: list[AuditRecord] = []
        for seq in reversed(index):
            record = self._get(int(seq))
            if record is None:
                continue
            if tenant_id is not None and record.tenant_id != tenant_id:
                continue
            records.append(record)
            if len(records) >= limit:
                break
        return records

    def verify_chain(self) -> dict[str, object]:
        """从 seq=1 起重算哈希，检测链条是否被篡改。"""
        head = self.head_seq()
        prev_hash = _GENESIS
        checked = 0
        for seq in range(1, head + 1):
            record = self._get(seq)
            if record is None:
                continue  # TTL 过期或缺失，跳过但不判定为篡改
            expected = _compute_hash(record.payload())
            if record.hash != expected or record.prev_hash != prev_hash:
                return {
                    "valid": False,
                    "checked": checked,
                    "broken_at": seq,
                    "head": head,
                }
            prev_hash = record.hash
            checked += 1
        return {"valid": True, "checked": checked, "broken_at": None, "head": head}
