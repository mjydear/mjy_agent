"""
📦 模块名称：Git 操作工具（Git Tools）
📍 架构位置：内置开发者工具集（Builtin Developer Tools）：
              [Agent ToolCall] → 【Git Tools】 → [SecuritySandbox] → git
🎯 核心作用：让 Agent 能查看 Git 状态、差异和历史，同时所有命令都经过安全沙箱审计。
🔗 依赖关系：
    - 依赖：SecuritySandbox、ToolRegistry
    - 被依赖：开发者工作流、代码审查、变更摘要生成
💡 设计思路：
    Git 工具不直接调用 subprocess，而是走 SecuritySandbox.run_shell()：
    ① 继承沙箱的命令白名单和超时控制
    ② 每次 git 操作都有 AuditRecord
    ③ path 参数仍然做 workspace 越界检查

📚 学习重点：
    1. 为什么只提供 status/diff/log 这类只读工具：默认安全边界应该保守
    2. 为什么 git diff(path) 要检查路径越界
    3. 为什么 sandbox 通过参数注入：测试时可以注入 fake sandbox
"""

from __future__ import annotations

from pathlib import Path

from athena.tools import SecuritySandbox, ToolRegistry


def register_git_tools(registry: ToolRegistry, workspace_root: Path | None = None, sandbox: SecuritySandbox | None = None) -> None:
    """注册经过沙箱审计的 Git 只读工具。"""

    root = (workspace_root or Path.cwd()).resolve()
    safe_sandbox = sandbox or SecuritySandbox()

    @registry.register
    async def git_status() -> str:
        """Return concise git status for the workspace."""
        result = await safe_sandbox.run_shell("git status --short", cwd=root)
        if not result.success:
            raise RuntimeError(result.error or "git status failed")
        return result.output

    @registry.register
    async def git_diff(path: str = "") -> str:
        """Return git diff for the workspace or one relative path."""
        if not isinstance(path, str):
            raise ValueError("path must be a string")
        command = "git diff --"
        if path.strip():
            target = (root / path).resolve()
            if root not in target.parents and target != root:
                raise PermissionError("path escapes workspace root")
            command = f"git diff -- {target.relative_to(root).as_posix()}"
        result = await safe_sandbox.run_shell(command, cwd=root)
        if not result.success:
            raise RuntimeError(result.error or "git diff failed")
        return result.output

    @registry.register
    async def git_log(limit: int = 5) -> str:
        """Return recent git commits."""
        if limit <= 0 or limit > 50:
            raise ValueError("limit must be in range 1..50")
        result = await safe_sandbox.run_shell(f"git log --oneline -n {limit}", cwd=root)
        if not result.success:
            raise RuntimeError(result.error or "git log failed")
        return result.output