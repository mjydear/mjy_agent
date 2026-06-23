"""Short-term working memory for the MVP agent loop."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from pydantic import BaseModel, Field, PositiveInt

logger = logging.getLogger(__name__)


class Message(BaseModel):
    """A message retained in working memory."""

    role: str
    content: str
    importance: float = 1.0


class WorkingMemory(BaseModel):
    """Token-aware sliding-window memory for short conversations."""

    max_tokens: PositiveInt = 8000
    messages: list[Message] = Field(default_factory=list)

    def add_message(self, role: str, content: str, importance: float = 1.0) -> None:
        """Add a message and prune old low-priority context if needed.

        Args:
            role: Chat role such as ``user``, ``assistant`` or ``tool``.
            content: Message body.
            importance: Relative retention priority.
        """
        self.messages.append(Message(role=role, content=content, importance=importance))
        self._prune_if_needed()

    def recent_messages(self) -> Sequence[Message]:
        """Return retained messages in chronological order."""
        return tuple(self.messages)

    def render(self) -> str:
        """Render memory as a compact prompt section."""
        return "\n".join(
            f"{message.role}: {message.content}" for message in self.messages
        )

    def _prune_if_needed(self) -> None:
        """Drop oldest low-importance messages when the rough token budget is exceeded."""
        while self._estimated_tokens() > self.max_tokens and len(self.messages) > 1:
            removable_index = min(
                range(len(self.messages) - 1),
                key=lambda index: self.messages[index].importance,
            )
            removed = self.messages.pop(removable_index)
            logger.debug("Pruned working-memory message role=%s", removed.role)

    def _estimated_tokens(self) -> int:
        """Estimate tokens cheaply; detailed tokenization is a phase-two enhancement."""
        return sum(max(1, len(message.content) // 4) for message in self.messages)
