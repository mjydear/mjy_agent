"""
Demo 1 - code intelligence assistant.

This script parses a project file with Athena's Tree-sitter tool and prints a
deterministic unit-test draft, so the demo works without an external LLM key.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from athena.tools import ToolCall, ToolRegistry
from athena.tools.builtin.code import register_code_tools


async def main() -> None:
    """Run the code analysis demo."""
    workspace = Path(__file__).resolve().parents[1]
    target = "athena/tools/registry.py"
    registry = ToolRegistry()
    register_code_tools(registry, workspace_root=workspace)

    outline = await registry.invoke(
        ToolCall(name="parse_code_outline", arguments={"path": target})
    )
    if not outline.success:
        raise RuntimeError(outline.error or "parse_code_outline failed")

    print("# Demo 1: Code Analysis -> Unit Test Draft")
    print(f"Target: {target}")
    print("\n## Syntax Outline")
    print("\n".join(outline.content.splitlines()[:24]))
    print("\n## Generated Test Draft")
    print("""async def test_tool_registry_reports_missing_required_argument() -> None:
    registry = ToolRegistry()

    @registry.register
    def echo(text: str) -> str:
        \"\"\"Return text unchanged.\"\"\"
        return text

    result = await registry.invoke(ToolCall(name=\"echo\", arguments={}))
    assert result.success is False
    assert \"Missing required argument\" in str(result.error)
""")


if __name__ == "__main__":
    asyncio.run(main())
