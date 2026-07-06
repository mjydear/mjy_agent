"""
会话持久化存储：把会话元数据与消息历史落到缓存后端（Redis/内存）。

企业级诉求：服务重启不丢会话、多副本共享会话。Agent 运行态（工作记忆）仍按进程重建，
会话历史由 messages 承载，因此重建后对话上下文可延续。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field

from athena.infra.cache import CacheBackend, cache_get_json, cache_set_json

_INDEX_KEY = "session:index"


@dataclass
class StoredMessage:
    """持久化的一条会话消息。"""

    role: str
    content: str
    created_at: float = field(default_factory=time.time)


@dataclass
class StoredSession:
    """持久化的会话记录（不含运行态 Agent）。"""

    session_id: str
    title: str
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    messages: list[StoredMessage] = field(default_factory=list)


class SessionStore:
    """
    基于缓存后端的会话仓库。

    键结构：session:{id} 存单会话 JSON；session:index 存全部会话 id 列表。
    Redis 可用时天然跨副本共享；降级内存时行为一致（仅单进程）。
    """

    def __init__(self, cache: CacheBackend, ttl_seconds: int = 3600) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._cache = cache
        self._ttl = ttl_seconds

    def _key(self, session_id: str) -> str:
        return f"session:{session_id}"

    def _index(self) -> list[str]:
        return cache_get_json(self._cache, _INDEX_KEY) or []

    def _write_index(self, ids: Sequence[str]) -> None:
        # 索引不设 TTL，避免整体列表提前过期；失效会话在读取时被动剔除
        cache_set_json(self._cache, _INDEX_KEY, list(dict.fromkeys(ids)))

    def _add_to_index(self, session_id: str) -> None:
        ids = self._index()
        if session_id not in ids:
            ids.append(session_id)
            self._write_index(ids)

    def create(self, session_id: str, title: str) -> StoredSession:
        session = StoredSession(session_id=session_id, title=title)
        self.save(session)
        return session

    def get(self, session_id: str) -> StoredSession | None:
        raw = cache_get_json(self._cache, self._key(session_id))
        if raw is None:
            return None
        return StoredSession(
            session_id=raw["session_id"],
            title=raw["title"],
            created_at=raw.get("created_at", time.time()),
            updated_at=raw.get("updated_at", time.time()),
            messages=[StoredMessage(**m) for m in raw.get("messages", [])],
        )

    def save(self, session: StoredSession) -> None:
        session.updated_at = time.time()
        cache_set_json(
            self._cache, self._key(session.session_id), asdict(session), ttl_seconds=self._ttl
        )
        self._add_to_index(session.session_id)

    def delete(self, session_id: str) -> None:
        self._cache.delete(self._key(session_id))
        ids = [sid for sid in self._index() if sid != session_id]
        self._write_index(ids)

    def list(self) -> list[StoredSession]:
        """列出全部会话，顺带剔除已过期（读不到）的索引项，按更新时间倒序。"""
        alive: list[StoredSession] = []
        surviving_ids: list[str] = []
        for sid in self._index():
            session = self.get(sid)
            if session is not None:
                alive.append(session)
                surviving_ids.append(sid)
        if len(surviving_ids) != len(self._index()):
            self._write_index(surviving_ids)
        return sorted(alive, key=lambda s: s.updated_at, reverse=True)
