"""
📦 模块名称：MCP 工具协议适配器（MCP Tool Adapter）
📍 架构位置：协议兼容层，位于 Athena ToolRegistry 和外部 MCP 风格工具定义之间。
🎯 核心作用：把 Athena 内部工具导出为 MCP 风格 schema，并把 MCP 调用解析成统一 ToolCall。
🔗 依赖关系：依赖 ToolRegistry、ToolCall、JSONValue；被协议兼容测试和未来 MCP Server 调用。
💡 设计思路：使用适配器模式，外部协议怎么变都不影响内部工具执行模型。
📚 学习重点：理解“内部模型稳定，外部协议通过 Adapter 转换”。
"""

from __future__ import annotations

from athena.tools import ToolCall, ToolRegistry
from athena.types import JSONValue


class MCPToolAdapter:
    """
    把 Athena ToolRegistry 适配为 MCP 风格定义。

    功能说明：负责导出工具 schema 和解析外部调用。
    参数说明：无构造参数。
    返回值：export_tools() 返回 schema 列表；parse_call() 返回 ToolCall。
    设计思路：不要让 ToolRegistry 直接理解 MCP，避免核心代码被协议细节污染。
    使用示例：tools = MCPToolAdapter().export_tools(registry)

    🎯 面试考点：什么是适配器模式？答案：把一个接口转换成另一个接口，保护系统内部模型不被外部格式牵着走。
    """

    def export_tools(self, registry: ToolRegistry) -> list[dict[str, JSONValue]]:
        """
        导出 MCP 风格工具定义。

        功能说明：遍历 ToolRegistry，把内部工具元数据转换为 MCP schema。
        参数说明：registry 是 Athena 的工具注册中心。
        返回值：MCP 风格工具定义列表。
        设计思路：导出时只读取元数据，不执行工具，保持协议层无副作用。
        使用示例：adapter.export_tools(registry)
        """
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "inputSchema": {"type": "object", "properties": dict(tool.parameters)},
            }
            for tool in registry.tools.values()
        ]

    def parse_call(self, payload: dict[str, JSONValue]) -> ToolCall:
        """
        解析 MCP 风格调用请求。

        功能说明：把外部 payload 转换为内部统一 ToolCall。
        参数说明：payload 通常包含 name 和 arguments。
        返回值：ToolCall。
        设计思路：所有协议最终都转成 ToolCall，ToolExecutor 就不需要关心来源协议。
        使用示例：adapter.parse_call({"name": "echo", "arguments": {"text": "hi"}})
        """
        name = payload.get("name")
        arguments = payload.get("arguments", {})
        if not isinstance(name, str) or not name.strip():
            raise ValueError("MCP call name must be a non-empty string")
        if not isinstance(arguments, dict):
            raise ValueError("MCP arguments must be an object")
        return ToolCall(name=name, arguments=arguments)


"""
🤔 思考题：

1. 如果 MCP 协议以后字段名变化，应该改 Adapter 还是 ToolRegistry？
2. 为什么 parse_call 要做类型检查？
3. 如果 arguments 中包含嵌套对象，当前 JSONValue 类型是否足够表达？
4. ⚡ 优化建议：未来可以把参数 schema 做得更精确，而不是简单 dict(tool.parameters)。
"""
