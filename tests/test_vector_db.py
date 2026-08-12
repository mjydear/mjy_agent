"""Tests for vector-store fallback."""

from __future__ import annotations

import pytest

from athena.infra.vector_db import InMemoryVectorStore, MemoryDocument


@pytest.mark.asyncio
async def test_in_memory_vector_store_searches_by_similarity() -> None:
    """In-memory vector store should return the nearest document first."""
    store = InMemoryVectorStore()
    await store.add(MemoryDocument(doc_id="a", content="near", embedding=[1.0, 0.0]))
    await store.add(MemoryDocument(doc_id="b", content="far", embedding=[0.0, 1.0]))

    results = await store.search([0.9, 0.1], top_k=1)

    assert results[0].doc_id == "a"
