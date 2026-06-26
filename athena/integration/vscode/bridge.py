"""
📦 模块名称：VS Code 命令桥接原型（VS Code Bridge）
📍 架构位置：外部集成层，位于 Athena Core 和未来 VS Code 插件之间。
🎯 核心作用：定义 VS Code 插件可以暴露的 Athena 命令元数据。
🔗 依赖关系：只依赖 dataclass；未来可被 VS Code extension scaffold 或 CLI 集成读取。
💡 设计思路：先定义稳定的命令契约，不急着生成完整插件，避免过早绑定前端实现。
📚 学习重点：理解“集成原型”先定边界，再逐步接真实插件。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VSCodeCommand:
    """
    VS Code 插件侧可触发的命令。

    功能说明：描述一个 VS Code command id 和显示标题。
    参数说明：command_id 是命令唯一 id；title 是命令面板显示文本。
    返回值：数据容器。
    设计思路：先把命令定义做成 Python 对象，未来可导出到 package.json。
    使用示例：VSCodeCommand("athena.chat", "Athena: Chat")
    """

    command_id: str
    title: str

    def __post_init__(self) -> None:
        if not self.command_id.strip() or not self.title.strip():
            raise ValueError("command_id and title must be non-empty")


class VSCodeIntegration:
    """
    VS Code 集成原型：只定义命令和任务触发格式。

    功能说明：返回 Athena 计划暴露给 VS Code 的命令列表。
    参数说明：无构造参数。
    返回值：commands() 返回 VSCodeCommand 元组。
    设计思路：这是“契约先行”，先让后端知道要支持哪些入口。
    使用示例：VSCodeIntegration().commands()
    """

    def commands(self) -> tuple[VSCodeCommand, ...]:
        """
        返回可暴露给插件的命令列表。

        功能说明：列出聊天和执行任务两个最小命令。
        参数说明：无。
        返回值：VSCodeCommand 元组。
        设计思路：元组不可变，适合作为固定命令清单。
        使用示例：for command in VSCodeIntegration().commands(): print(command.command_id)
        """
        return (
            VSCodeCommand(command_id="athena.chat", title="Athena: Chat"),
            VSCodeCommand(command_id="athena.runTask", title="Athena: Run Task"),
        )


"""
🤔 思考题：

1. 如果要做真正 VS Code 插件，还需要哪些 TypeScript 文件？
2. 命令 id 为什么要使用 athena.xxx 命名空间？
3. 这些命令最终应该调用 CLI、HTTP 服务还是本地 Python 进程？
4. ⚡ 优化建议：未来可以提供 export_package_json_contributes() 自动生成插件 contribution 配置。
"""
