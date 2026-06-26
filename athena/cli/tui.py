"""
📦 模块名称：富交互终端界面（Textual TUI）
📍 架构位置：用户接口层（UI Layer）—— 与 main.py 的 Typer CLI 平级：
              [Terminal User] → 【Textual TUI】 → [ReActAgent]
🎯 核心作用：提供比普通 REPL 更友好的终端交互体验，包括滚动日志、输入框和状态栏。
🔗 依赖关系：
    - 依赖：Textual、ReActAgent
    - 被依赖：athena cli main.py 中的 `athena tui` 命令
💡 设计思路：
    TUI 使用懒加载 Textual：只有用户真正运行 `athena tui` 时才 import，
    这样普通 `athena chat/start` 不会因为终端 UI 依赖问题受影响。

📚 学习重点：
    1. 为什么 agent_factory 通过参数注入：TUI 不负责读取配置，只负责交互
    2. 为什么 on_input_submitted 使用 asyncio.wait_for：用户界面不能无限等待一个请求
    3. 为什么 RichLog 适合展示对话：支持滚动、换行、高亮和富文本
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from athena.agent import ReActAgent


def run_tui(agent_factory: Callable[[], ReActAgent]) -> None:
    """
    启动 Textual TUI。

    参数说明：
        agent_factory: 创建 ReActAgent 的工厂函数，由 CLI 组合根传入，保持依赖注入风格。
    """
    try:
        from textual.app import App, ComposeResult
        from textual.containers import Vertical
        from textual.widgets import Footer, Header, Input, RichLog
    except ImportError as exc:
        raise RuntimeError(
            "Textual is required to run the TUI. Install textual first."
        ) from exc

    class AthenaTUI(App[None]):
        CSS = """
        Screen { layout: vertical; }
        #log { height: 1fr; border: solid $accent; }
        #input { dock: bottom; }
        """

        def __init__(self) -> None:
            super().__init__()
            self.agent = agent_factory()

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with Vertical():
                yield RichLog(id="log", wrap=True, highlight=True, markup=True)
                yield Input(placeholder="Ask Athena...", id="input")
            yield Footer()

        async def on_input_submitted(self, event: Input.Submitted) -> None:
            query = event.value.strip()
            event.input.value = ""
            if not query:
                return
            log = self.query_one("#log", RichLog)
            log.write(f"[bold cyan]You[/]: {query}")
            try:
                response = await asyncio.wait_for(self.agent.run(query), timeout=120)
                log.write(f"[bold green]Athena[/]: {response.answer}")
            except Exception as exc:
                log.write(f"[bold red]Error[/]: {exc}")

    AthenaTUI().run()
