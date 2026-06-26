"""
📦 模块名称：OpenAI 工具调用适配器（OpenAI Tool Adapter）
📍 架构位置：协议兼容层，位于 Athena ToolRegistry 和 OpenAI function calling 格式之间。
🎯 核心作用：导出 OpenAI tools 格式，并把 OpenAI tool_call 解析成统一 ToolCall。
🔗 依赖关系：依赖 json、ToolRegistry、ToolCall、JSONValue；被协议测试和未来 LLM 接入层调用。
💡 设计思路：同样使用适配器模式，让外部 LLM 的 tool_call 格式不侵入内部工具系统。
📚 学习重点：注意 OpenAI 的 arguments 是 JSON 字符串，不是 Python dict，这是常见踩坑点。
"""

from __future__ import annotations

import json

from athena.tools import ToolCall, ToolRegistry
from athena.types import JSONValue


class OpenAIToolAdapter:
    """
    把 Athena 工具适配为 OpenAI tools/function calling 格式。

    功能说明：负责 Athena 工具和 OpenAI 工具格式之间的双向转换。
    参数说明：无构造参数。
    返回值：export_tools() 返回 OpenAI tools 列表；parse_call() 返回 ToolCall。
    设计思路：协议适配集中在一个类里，内部 ToolExecutor 永远只接收 ToolCall。
    使用示例：OpenAIToolAdapter().parse_call(payload)
    """

    def export_tools(self, registry: ToolRegistry) -> list[dict[str, JSONValue]]:
        """
        导出 OpenAI tools 格式。

        功能说明：把 ToolRegistry 中的工具转换成 OpenAI 期望的 function schema。
        参数说明：registry 是工具注册中心。
        返回值：OpenAI tools 列表。
        设计思路：只做格式转换，不绑定任何 OpenAI SDK，保持项目轻量。
        使用示例：tools = adapter.export_tools(registry)
        """
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": {
                        "type": "object",
                        "properties": dict(tool.parameters),
                    },
                },
            }
            for tool in registry.tools.values()
        ]

    def parse_call(self, payload: dict[str, JSONValue]) -> ToolCall:
        """
        解析 OpenAI tool_call 格式。

        功能说明：从 payload.function 中取出 name 和 arguments，并转为 ToolCall。
        参数说明：payload 是 OpenAI 返回的单个 tool_call 对象。
        返回值：ToolCall。
        设计思路：在边界处立即解析 JSON 字符串，内部代码只处理 dict，降低复杂度。
        使用示例：adapter.parse_call({"function": {"name": "echo", "arguments": "{\"text\": \"hi\"}"}})

        🔍 原理讲解：
        输入：OpenAI tool_call，其中 arguments 是字符串。
        处理过程：校验 function → 取 name → json.loads(arguments) → 生成 ToolCall。
        输出：ToolCall(name="echo", arguments={"text": "hi"})。
        """
        function = payload.get("function")
        if not isinstance(function, dict):
            raise ValueError("OpenAI tool call must contain function object")
        name = function.get("name")
        raw_arguments = function.get("arguments", "{}")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("OpenAI function name must be non-empty")
        if not isinstance(raw_arguments, str):
            raise ValueError("OpenAI function arguments must be a JSON string")
        parsed = json.loads(
            raw_arguments
        )  # 💡 学习提示：OpenAI arguments 常见坑是“看起来像对象，实际是 JSON 字符串”。
        if not isinstance(parsed, dict):
            raise ValueError("OpenAI function arguments must decode to an object")
        return ToolCall(name=name, arguments=parsed)


"""
🤔 思考题：

1. 如果 json.loads 失败，当前会抛 JSONDecodeError；上层应该如何转成用户可读错误？
2. 为什么不让 ToolExecutor 直接解析 OpenAI payload？
3. MCP 和 OpenAI Adapter 有相似逻辑，是否应该抽象父类？什么时候值得抽象？
4. ⚡ 优化建议：未来可以给 export_tools 增加 required 字段，让 LLM 更清楚哪些参数必须提供。
"""
