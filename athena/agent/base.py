"""Agent base abstractions."""

from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, Field


class AgentResponse(BaseModel):
    """Response returned by an Athena agent."""

    answer: str
    steps: list[str] = Field(default_factory=list)


class Agent(Protocol):
    """Protocol for asynchronous Athena agents."""

    async def run(self, query: str) -> AgentResponse:
        """Run the agent for a user query."""
