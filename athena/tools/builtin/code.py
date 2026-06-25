"""
📦 模块名称：代码解析工具（Code Parsing Tools）
📍 架构位置：内置开发者工具集（Builtin Developer Tools）：
              [Agent ToolCall] → 【parse_code_outline】 → [Tree-sitter]
🎯 核心作用：用 Tree-sitter 解析源代码结构，避免用正则硬猜函数、类和语法节点。
🔗 依赖关系：
    - 依赖：tree-sitter-language-pack、ToolRegistry
    - 被依赖：代码理解、代码搜索、重构辅助等开发者工具场景
💡 设计思路：
    Tree-sitter 负责把代码解析成 AST；本工具只输出紧凑 outline，供 Agent 快速理解文件结构。
    这比正则更可靠，因为它理解语言语法，可以区分注释、字符串、嵌套结构和真实语法节点。

📚 学习重点：
    1. 为什么代码解析一定要用语法树，而不是正则
    2. LANGUAGE_BY_SUFFIX 如何把文件后缀映射到解析器语言
    3. 为什么 outline 限制深度和行数：控制 Token 消耗，避免把整棵 AST 塞进上下文
"""

from __future__ import annotations

from pathlib import Path

from athena.tools import ToolRegistry


LANGUAGE_BY_SUFFIX = {".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx", ".java": "java", ".c": "c", ".cpp": "cpp"}


def register_code_tools(registry: ToolRegistry, workspace_root: Path | None = None) -> None:
    """注册基于 Tree-sitter 的代码结构查看工具。"""

    root = (workspace_root or Path.cwd()).resolve()

    def resolve(path: str) -> Path:
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        target = (root / path).resolve()
        if root not in target.parents and target != root:
            raise PermissionError("path escapes workspace root")
        return target

    @registry.register
    def parse_code_outline(path: str) -> str:
        """Parse source code with Tree-sitter and return a compact syntax outline."""
        target = resolve(path)
        if not target.is_file():
            raise FileNotFoundError(path)
        language = LANGUAGE_BY_SUFFIX.get(target.suffix.lower())
        if language is None:
            raise ValueError(f"unsupported source suffix: {target.suffix}")
        parser = _load_parser(language)
        source = target.read_bytes()
        tree = parser.parse(source)
        lines: list[str] = []
        _walk(tree.root_node, lines, max_depth=3)
        return "\n".join(lines[:200])


def _load_parser(language: str):
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:
        raise RuntimeError("tree-sitter-language-pack is required for code parsing") from exc
    return get_parser(language)


def _walk(node, lines: list[str], depth: int = 0, max_depth: int = 3) -> None:
    if depth > max_depth:
        return
    lines.append(f"{'  ' * depth}{node.type} [{node.start_point.row + 1}:{node.start_point.column + 1}]")
    for child in node.children:
        if child.is_named:
            _walk(child, lines, depth + 1, max_depth)