"""Enhanced memory behavior tests."""

from __future__ import annotations

import pytest

from athena.infra.vector_db import InMemoryVectorStore
from athena.memory import LongTermMemory, WorkingMemory


def test_working_memory_compresses_before_pruning() -> None:
    """Low-importance long messages should be summarized before removal is needed."""
    memory = WorkingMemory(max_tokens=80, compression_threshold=0.5)
    memory.add_message("tool", "detail " * 80, importance=0.1)
    memory.add_message("user", "keep this important requirement", importance=2.0)

    rendered = memory.render()
    assert "[compressed]" in rendered
    assert "keep this important requirement" in rendered
    assert len(rendered) < len("detail " * 80)


def test_working_memory_rejects_invalid_importance() -> None:
    memory = WorkingMemory(max_tokens=100)

    with pytest.raises(ValueError):
        memory.add_message("user", "hello", importance=-1.0)


@pytest.mark.asyncio
async def test_long_term_memory_hybrid_retrieval_prefers_importance() -> None:
    memory = LongTermMemory(InMemoryVectorStore())
    await memory.add("low", "python testing notes", importance=0.1)
    await memory.add("high", "python testing notes", importance=5.0)

    records = await memory.search("python testing", top_k=1)

    assert records[0].doc_id == "high"