"""
📦 模块名称：文件操作工具（File Tools）
📍 架构位置：内置开发者工具集（Builtin Developer Tools）：
              [Agent ToolCall] → [ToolRegistry] → 【read/write/list file tools】
🎯 核心作用：为 Agent 提供受控的文件读取、写入和列表能力，支持开发者生产力场景。
🔗 依赖关系：
    - 依赖：pathlib、ToolRegistry
    - 被依赖：CLI 组装根、未来开发者工具包注册流程
💡 设计思路：
    文件工具的核心不是“能读写文件”，而是“安全地读写工作区内文件”：
    ① resolve() 统一做路径归一化和越界检查
    ② write_text_file() 覆盖前生成 .bak 备份，为回滚留后路
    ③ max_chars / limit 控制输出规模，避免把巨大文件塞进 LLM 上下文

📚 学习重点：
    1. 为什么路径必须 resolve 后检查 workspace_root
    2. 为什么写入要生成备份：外部操作必须可审计、可回滚
    3. 为什么工具函数注册在 register_file_tools 内部：便于注入 workspace_root
"""

from __future__ import annotations

from pathlib import Path

from athena.tools import ToolRegistry


def register_file_tools(registry: ToolRegistry, workspace_root: Path | None = None) -> None:
    """注册受工作区边界保护的文件工具。"""

    root = (workspace_root or Path.cwd()).resolve()

    def resolve(path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        target = (root / path).resolve()
        if root not in target.parents and target != root:
            raise PermissionError("path escapes workspace root")
        return target

    @registry.register
    def read_text_file(path: str, max_chars: int = 20000) -> str:
        """Read a UTF-8 text file from the workspace."""
        if max_chars <= 0:
            raise ValueError("max_chars must be positive")
        target = resolve(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        return target.read_text(encoding="utf-8")[:max_chars]

    @registry.register
    def write_text_file(path: str, content: str) -> str:
        """Write a UTF-8 text file and create a rollback backup when replacing content."""
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        target = resolve(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        backup_path = target.with_suffix(target.suffix + ".bak")
        if target.exists():
            backup_path.write_text(target.read_text(encoding="utf-8"), encoding="utf-8")
        target.write_text(content, encoding="utf-8")
        return f"wrote {target.relative_to(root)} backup={backup_path.exists()}"

    @registry.register
    def list_workspace_files(path: str = ".", limit: int = 200) -> str:
        """List files under a workspace directory."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        target = resolve(path)
        if not target.is_dir():
            raise NotADirectoryError(path)
        entries = sorted(child.relative_to(root).as_posix() for child in target.rglob("*") if child.is_file())
        return "\n".join(entries[:limit])