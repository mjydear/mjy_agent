"""Vector memory storage abstractions and MVP implementations."""

from __future__ import annotations

import logging
import asyncio
from collections.abc import Sequence
from typing import Protocol, cast

from pydantic import BaseModel, Field

from athena.exceptions import ErrorCode, VectorStoreError

logger = logging.getLogger(__name__)


class MemoryDocument(BaseModel):
    """A document stored in vector memory."""

    doc_id: str
    content: str
    embedding: Sequence[float]
    metadata: dict[str, str] = Field(default_factory=dict)


class VectorStore(Protocol):
    """Protocol for vector memory stores."""

    async def add(self, document: MemoryDocument) -> None:
        """Persist one memory document."""

    async def search(self, embedding: Sequence[float], top_k: int) -> Sequence[MemoryDocument]:
        """Return the most relevant memory documents."""


class InMemoryVectorStore(BaseModel):
    """Deterministic vector-store fallback for tests and local MVP demos."""

    documents: list[MemoryDocument] = Field(default_factory=list)

    async def add(self, document: MemoryDocument) -> None:
        """Store a document in memory."""
        self.documents.append(document)

    async def search(self, embedding: Sequence[float], top_k: int) -> Sequence[MemoryDocument]:
        """Search documents by cosine similarity."""
        ranked = sorted(
            self.documents,
            key=lambda document: _cosine_similarity(embedding, document.embedding),
            reverse=True,
        )
        return ranked[:top_k]


class MilvusVectorStore(BaseModel):
    """Milvus adapter skeleton behind the VectorStore protocol.

    The MVP keeps Milvus isolated here so the agent loop can be tested without
    an external service. Production collection creation and HNSW tuning belong
    in this adapter, not in memory callers.
    """

    uri: str = "http://localhost:19530"
    collection_name: str = "athena_memory"
    dimension: int = 1536
    metric_type: str = "COSINE"

    async def add(self, document: MemoryDocument) -> None:
        """Persist a document to Milvus using PyMilvus MilvusClient.

        Raises:
            VectorStoreError: If the Milvus operation fails.
        """
        try:
            await asyncio.to_thread(self._add_sync, document)
        except Exception as exc:
            logger.exception("Milvus add failed")
            raise VectorStoreError(ErrorCode.VECTOR_STORE_FAILED, str(exc)) from exc

    async def search(self, embedding: Sequence[float], top_k: int) -> Sequence[MemoryDocument]:
        """Search Milvus for relevant memories using vector similarity.

        Raises:
            VectorStoreError: If the Milvus operation fails.
        """
        try:
            return await asyncio.to_thread(self._search_sync, embedding, top_k)
        except Exception as exc:
            logger.exception("Milvus search failed")
            raise VectorStoreError(ErrorCode.VECTOR_STORE_FAILED, str(exc)) from exc

    def _add_sync(self, document: MemoryDocument) -> None:
        """Run the blocking Milvus insert path."""
        client = self._client()
        self._ensure_collection(client)
        client.insert(
            collection_name=self.collection_name,
            data=[
                {
                    "id": document.doc_id,
                    "vector": list(document.embedding),
                    "content": document.content,
                    "metadata": document.metadata,
                }
            ],
        )

    def _search_sync(self, embedding: Sequence[float], top_k: int) -> Sequence[MemoryDocument]:
        """Run the blocking Milvus search path."""
        client = self._client()
        self._ensure_collection(client)
        raw_results = client.search(
            collection_name=self.collection_name,
            data=[list(embedding)],
            limit=top_k,
            output_fields=["content", "metadata"],
        )
        first_page = cast(Sequence[object], raw_results[0] if raw_results else [])
        documents: list[MemoryDocument] = []
        for item in first_page:
            item_map = cast(dict[str, object], item)
            entity = cast(dict[str, object], item_map.get("entity", {}))
            metadata_value = entity.get("metadata", {})
            metadata = metadata_value if isinstance(metadata_value, dict) else {}
            documents.append(
                MemoryDocument(
                    doc_id=str(item_map.get("id", "")),
                    content=str(entity.get("content", "")),
                    embedding=list(embedding),
                    metadata={str(key): str(value) for key, value in metadata.items()},
                )
            )
        return documents

    def _client(self) -> object:
        """Create a Milvus client lazily to keep tests independent of Milvus."""
        from pymilvus import MilvusClient

        return MilvusClient(uri=self.uri)

    def _ensure_collection(self, client: object) -> None:
        """Create the MVP collection if it does not already exist."""
        milvus_client = cast("MilvusClientProtocol", client)
        if milvus_client.has_collection(self.collection_name):
            return
        milvus_client.create_collection(
            collection_name=self.collection_name,
            dimension=self.dimension,
            primary_field_name="id",
            vector_field_name="vector",
            metric_type=self.metric_type,
            auto_id=False,
        )


class MilvusClientProtocol(Protocol):
    """Small protocol for the PyMilvus methods Athena uses."""

    def has_collection(self, collection_name: str) -> bool:
        """Return whether a collection exists."""

    def create_collection(
        self,
        collection_name: str,
        dimension: int,
        primary_field_name: str,
        vector_field_name: str,
        metric_type: str,
        auto_id: bool,
    ) -> None:
        """Create a Milvus collection."""

    def insert(self, collection_name: str, data: Sequence[dict[str, object]]) -> object:
        """Insert entities into a collection."""

    def search(
        self,
        collection_name: str,
        data: Sequence[Sequence[float]],
        limit: int,
        output_fields: Sequence[str],
    ) -> Sequence[Sequence[object]]:
        """Search a collection by vector."""


def _cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Compute cosine similarity for same-length vectors."""
    if len(left) != len(right) or not left or not right:
        return 0.0
    dot_product = sum(left_value * right_value for left_value, right_value in zip(left, right))
    left_norm = sum(value * value for value in left) ** 0.5
    right_norm = sum(value * value for value in right) ** 0.5
    if left_norm == 0.0 or right_norm == 0.0:
        return 0.0
    return dot_product / (left_norm * right_norm)
