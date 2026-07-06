"""向量库工厂测试：默认内存、milvus 连不上降级内存。"""

from __future__ import annotations

import athena.infra.vector_db as vdb
from athena.config import AthenaSettings, VectorStoreSettings
from athena.infra.vector_db import (
    InMemoryVectorStore,
    create_vector_store,
)


def test_default_backend_is_memory() -> None:
    store = create_vector_store(AthenaSettings())
    assert isinstance(store, InMemoryVectorStore)


def test_milvus_unavailable_falls_back_to_memory(monkeypatch) -> None:
    # 强制探测抛错（模拟连不上/未装 pymilvus）→ 降级内存，避免真实网络等待
    def _boom(self):
        raise ConnectionError("milvus down")

    monkeypatch.setattr(vdb.MilvusVectorStore, "_client", _boom)
    settings = AthenaSettings(
        vector_store=VectorStoreSettings(backend="milvus")
    )
    store = create_vector_store(settings)
    assert isinstance(store, InMemoryVectorStore)

