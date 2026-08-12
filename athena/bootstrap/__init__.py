"""Public composition roots shared by CLI, API and Worker entrypoints."""

from athena.bootstrap.agent_factory import build_agent

__all__ = ["build_agent"]
