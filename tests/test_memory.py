"""Tests for working memory."""

from __future__ import annotations

from athena.memory import WorkingMemory



def test_working_memory_retains_recent_messages() -> None:
    """Working memory should render retained messages."""
    memory = WorkingMemory(max_tokens=20)
    memory.add_message("user", "hello", importance=2.0)
    memory.add_message("assistant", "hi", importance=2.0)

    assert "user: hello" in memory.render()
    assert "assistant: hi" in memory.render()


def test_working_memory_prunes_low_importance_messages() -> None:
    """Working memory should prune old low-importance content first."""
    memory = WorkingMemory(max_tokens=4)
    memory.add_message("tool", "x" * 80, importance=0.1)
    memory.add_message("user", "keep", importance=2.0)

    rendered = memory.render()
    assert "keep" in rendered
    assert "x" * 80 not in rendered
