"""Tool protocol compatibility adapters."""

from athena.tools.protocol.mcp_adapter import MCPToolAdapter
from athena.tools.protocol.openai_adapter import OpenAIToolAdapter

__all__ = ["MCPToolAdapter", "OpenAIToolAdapter"]
