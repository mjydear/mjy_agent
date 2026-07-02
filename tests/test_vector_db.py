"""Tests for vector database implementations and embedding providers."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from athena.exceptions import ErrorCode, VectorStoreError
from athena.infra.vector_db import (
    InMemoryVectorStore,
    MemoryDocument,
    MilvusVectorStore,
)


# ============================================================
# InMemoryVectorStore Tests
# ============================================================


@pytest.mark.asyncio
async def test_in_memory_vector_store_searches_by_similarity() -> None:
    """In-memory vector store should return the nearest document first."""
    store = InMemoryVectorStore()
    await store.add(MemoryDocument(doc_id="a", content="near", embedding=[1.0, 0.0]))
    await store.add(MemoryDocument(doc_id="b", content="far", embedding=[0.0, 1.0]))
    results = await store.search([0.9, 0.1], top_k=1)
    assert results[0].doc_id == "a"


@pytest.mark.asyncio
async def test_search_empty_store_returns_empty() -> None:
    store = InMemoryVectorStore()
    results = await store.search(embedding=[1.0, 0.0], top_k=3)
    assert results == []


@pytest.mark.asyncio
async def test_search_returns_top_k_documents() -> None:
    store = InMemoryVectorStore()
    await store.add(MemoryDocument(doc_id="a", content="First", embedding=[1.0, 0.0]))
    await store.add(MemoryDocument(doc_id="b", content="Second", embedding=[0.9, 0.0]))
    await store.add(MemoryDocument(doc_id="c", content="Third", embedding=[0.0, 0.1]))
    results = await store.search(embedding=[1.0, 0.0], top_k=2)
    assert len(results) == 2
    assert results[0].content == "First"
    assert results[1].content == "Second"


@pytest.mark.asyncio
async def test_vector_dimension_mismatch_raises_error() -> None:
    store = InMemoryVectorStore()
    await store.add(MemoryDocument(doc_id="a", content="First", embedding=[1.0, 0.0]))
    with pytest.raises(VectorStoreError) as exc_info:
        await store.search(embedding=[1.0, 0.0, 0.0], top_k=1)
    assert exc_info.value.code == ErrorCode.VECTOR_STORE_FAILED


# ============================================================
# MilvusVectorStore Tests
# ============================================================


@pytest.mark.asyncio
async def test_milvus_caches_client_instance() -> None:
    """MilvusVectorStore should reuse the client instance across calls."""
    store = MilvusVectorStore(uri="http://localhost:9999", dimension=128)

    mock_client = MagicMock()
    mock_client.has_collection.return_value = True
    mock_client.insert.return_value = {}

    with patch("athena.infra.vector_db.MilvusClient", return_value=mock_client):
        await store.add(
            MemoryDocument(doc_id="doc_1", content="test", embedding=[0.0] * 128)
        )
        first_client = store._client_instance

        await store.add(
            MemoryDocument(doc_id="doc_2", content="test2", embedding=[0.0] * 128)
        )
        assert store._client_instance is first_client


@pytest.mark.asyncio
async def test_milvus_creates_collection_with_hnsw_index() -> None:
    """MilvusVectorStore should create collection with HNSW index parameters."""
    store = MilvusVectorStore(
        uri="http://localhost:9999",
        dimension=1536,
        index_type="HNSW",
        index_params={"M": 16, "efConstruction": 200},
    )

    mock_client = MagicMock()
    mock_client.has_collection.return_value = False

    with patch("athena.infra.vector_db.MilvusClient", return_value=mock_client):
        await store.add(
            MemoryDocument(doc_id="doc_1", content="test", embedding=[0.0] * 1536)
        )

    mock_client.create_collection.assert_called_once()
    call_kwargs = mock_client.create_collection.call_args[1]
    assert call_kwargs["index_type"] == "HNSW"
    assert call_kwargs["index_params"] == {"M": 16, "efConstruction": 200}
    assert call_kwargs["dimension"] == 1536


@pytest.mark.asyncio
async def test_milvus_skips_collection_when_exists() -> None:
    """MilvusVectorStore should skip creation when collection already exists."""
    store = MilvusVectorStore(uri="http://localhost:9999", dimension=128)

    mock_client = MagicMock()
    mock_client.has_collection.return_value = True
    mock_client.insert.return_value = {}

    with patch("athena.infra.vector_db.MilvusClient", return_value=mock_client):
        await store.add(
            MemoryDocument(doc_id="doc_1", content="test", embedding=[0.0] * 128)
        )
        await store.add(
            MemoryDocument(doc_id="doc_2", content="test2", embedding=[0.0] * 128)
        )

    mock_client.create_collection.assert_not_called()
    assert mock_client.insert.call_count == 2


@pytest.mark.asyncio
async def test_milvus_search_returns_documents() -> None:
    """MilvusVectorStore search should return parsed MemoryDocuments."""
    store = MilvusVectorStore(uri="http://localhost:9999", dimension=128)

    mock_client = MagicMock()
    mock_client.has_collection.return_value = True
    mock_client.search.return_value = [
        [
            {
                "id": "doc_1",
                "distance": 0.95,
                "entity": {
                    "content": "Hello from Milvus",
                    "metadata": {"source": "test"},
                },
            }
        ]
    ]

    with patch("athena.infra.vector_db.MilvusClient", return_value=mock_client):
        results = await store.search(embedding=[0.0] * 128, top_k=5)

    assert len(results) == 1
    assert results[0].doc_id == "doc_1"
    assert results[0].content == "Hello from Milvus"
    assert results[0].metadata == {"source": "test"}


@pytest.mark.asyncio
async def test_milvus_add_failure_raises_vector_store_error() -> None:
    """MilvusVectorStore should wrap errors in VectorStoreError."""
    store = MilvusVectorStore(uri="http://localhost:9999", dimension=128)

    mock_client = MagicMock()
    mock_client.has_collection.return_value = True
    mock_client.insert.side_effect = RuntimeError("Connection refused")

    with patch("athena.infra.vector_db.MilvusClient", return_value=mock_client):
        with pytest.raises(VectorStoreError) as exc_info:
            await store.add(
                MemoryDocument(doc_id="doc_1", content="test", embedding=[0.0] * 128)
            )
        assert exc_info.value.code == ErrorCode.VECTOR_STORE_FAILED
        assert "Connection refused" in str(exc_info.value)


# ============================================================
# OpenAIEmbeddingProvider Tests
# ============================================================


class TestOpenAIEmbeddingProvider:
    """Tests for the OpenAI embedding provider."""

    @pytest.mark.asyncio
    async def test_embed_returns_vector(self) -> None:
        """OpenAIEmbeddingProvider should return an embedding vector."""
        from athena.memory.long_term import OpenAIEmbeddingProvider

        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.1] * 1536)]

        with patch(
            "athena.memory.long_term.aembedding", new_callable=AsyncMock
        ) as mock_aembed:
            mock_aembed.return_value = mock_response
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                provider = OpenAIEmbeddingProvider()
                embedding = await provider.embed("Hello world")

        assert len(embedding) == 1536
        assert embedding[0] == 0.1

    @pytest.mark.asyncio
    async def test_embed_raises_on_missing_api_key(self) -> None:
        """OpenAIEmbeddingProvider should raise when API key is missing."""
        from athena.memory.long_term import OpenAIEmbeddingProvider

        provider = OpenAIEmbeddingProvider()
        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(RuntimeError, match="Missing API key"):
                await provider.embed("Hello")

    @pytest.mark.asyncio
    async def test_embed_raises_on_empty_text(self) -> None:
        """OpenAIEmbeddingProvider should raise on empty text."""
        from athena.memory.long_term import OpenAIEmbeddingProvider

        provider = OpenAIEmbeddingProvider()
        with pytest.raises(ValueError, match="non-empty string"):
            await provider.embed("")

    @pytest.mark.asyncio
    async def test_embed_retries_on_failure(self) -> None:
        """OpenAIEmbeddingProvider should retry once on transient failure."""
        from athena.memory.long_term import OpenAIEmbeddingProvider

        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.0] * 1536)]

        call_count = 0

        async def mock_aembed(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Network timeout")
            return mock_response

        with patch("athena.memory.long_term.aembedding", side_effect=mock_aembed):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                provider = OpenAIEmbeddingProvider()
                embedding = await provider.embed("test")

        assert call_count == 2
        assert len(embedding) == 1536

    @pytest.mark.asyncio
    async def test_embed_with_custom_api_base(self) -> None:
        """OpenAIEmbeddingProvider should pass api_base to litellm."""
        from athena.memory.long_term import OpenAIEmbeddingProvider

        mock_response = MagicMock()
        mock_response.data = [MagicMock(embedding=[0.0] * 10)]

        with patch(
            "athena.memory.long_term.aembedding", new_callable=AsyncMock
        ) as mock_aembed:
            mock_aembed.return_value = mock_response
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                provider = OpenAIEmbeddingProvider(
                    model="text-embedding-3-small",
                    api_base="https://custom-proxy.example.com",
                )
                await provider.embed("test")

        call_kwargs = mock_aembed.call_args[1]
        assert call_kwargs["api_base"] == "https://custom-proxy.example.com"


# ============================================================
# HashEmbeddingProvider Tests
# ============================================================


class TestHashEmbeddingProvider:
    """Tests for the hash-based embedding provider."""

    @pytest.mark.asyncio
    async def test_embed_returns_fixed_dimension_vector(self) -> None:
        from athena.memory.long_term import HashEmbeddingProvider

        provider = HashEmbeddingProvider(dimension=64)
        embedding = await provider.embed("Hello world")
        assert len(embedding) == 64

    @pytest.mark.asyncio
    async def test_same_text_produces_same_embedding(self) -> None:
        from athena.memory.long_term import HashEmbeddingProvider

        provider = HashEmbeddingProvider(dimension=64)
        e1 = await provider.embed("Hello world")
        e2 = await provider.embed("Hello world")
        assert e1 == e2

    @pytest.mark.asyncio
    async def test_different_texts_produce_different_embeddings(self) -> None:
        from athena.memory.long_term import HashEmbeddingProvider

        provider = HashEmbeddingProvider(dimension=128)
        e1 = await provider.embed("Hello world")
        e2 = await provider.embed("Goodbye world")
        assert e1 != e2

    @pytest.mark.asyncio
    async def test_embedding_is_unit_vector(self) -> None:
        from athena.memory.long_term import HashEmbeddingProvider

        provider = HashEmbeddingProvider(dimension=64)
        embedding = await provider.embed("Hello world")
        norm = sum(v * v for v in embedding)
        assert abs(norm - 1.0) < 1e-6