"""
📦 模块名称：工具审计日志（Tool Audit Log）
📍 架构位置：工具执行层的可追溯组件，和权限管理、沙箱执行并列。
🎯 核心作用：记录每一次工具调用的动作、结果、时间和操作者。
🔗 依赖关系：依赖 json、time、Path；可被 ToolExecutor、PermissionManager、观测平台读取。
💡 设计思路：使用“追加式事件日志”模式，内存保存便于测试，JSONL 文件便于落盘追踪。
📚 学习重点：理解企业级 Agent 为什么必须能回答“谁在什么时候调用了什么工具”。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ToolAuditEvent:
    """
    工具调用审计事件。

    功能说明：描述一次工具相关动作。
    参数说明：
        tool_name：工具名。
        action：动作名称，例如 run、deny、confirm。
        success：动作是否成功。
        timestamp：事件时间戳。
        actor：操作者，默认 agent。
        detail：补充信息。
    返回值：数据容器。
    设计思路：事件越结构化，后续越容易搜索、统计和回放。
    使用示例：ToolAuditEvent("git_status", "run", True)
    """

    tool_name: str
    action: str
    success: bool
    timestamp: float = field(default_factory=time.time)
    actor: str = "agent"
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.tool_name.strip():
            raise ValueError("tool_name must be non-empty")
        if not self.action.strip():
            raise ValueError("action must be non-empty")


class AuditLogger:
    """
    JSONL 审计日志记录器。

    功能说明：把审计事件保存在内存中，并可选写入 JSONL 文件。
    参数说明：sink_path 是可选日志文件路径。
    返回值：record() 返回 None，by_tool() 返回事件元组。
    设计思路：JSONL 一行一个事件，适合持续追加，也方便命令行工具分析。
    使用示例：logger = AuditLogger(Path("audit.jsonl")); logger.record(event)
    """

    def __init__(self, sink_path: Path | None = None) -> None:
        self.sink_path = sink_path
        self.events: list[ToolAuditEvent] = []

    def record(self, event: ToolAuditEvent) -> None:
        """
        记录一条审计事件。

        功能说明：把事件加入内存列表，并在配置 sink_path 时写入文件。
        参数说明：event 是 ToolAuditEvent。
        返回值：None。
        设计思路：内存记录适合测试和 Web 展示，文件记录适合长期留痕。
        使用示例：logger.record(ToolAuditEvent("read", "run", True))
        """
        if not isinstance(event, ToolAuditEvent):
            raise ValueError("event must be a ToolAuditEvent")
        self.events.append(event)
        if self.sink_path is not None:
            self.sink_path.parent.mkdir(parents=True, exist_ok=True)
            with self.sink_path.open("a", encoding="utf-8") as stream:
                # 💡 学习提示：JSONL 比一个大 JSON 数组更适合审计日志，因为每次只追加一行，不需要重写整个文件。
                stream.write(json.dumps(event.__dict__, ensure_ascii=False) + "\n")

    def by_tool(self, tool_name: str) -> tuple[ToolAuditEvent, ...]:
        """
        按工具名查询审计事件。

        功能说明：从内存事件列表里筛选指定工具的记录。
        参数说明：tool_name 是工具名。
        返回值：匹配的 ToolAuditEvent 元组。
        设计思路：返回 tuple，避免调用方误改内部事件列表。
        使用示例：events = logger.by_tool("git_status")
        """
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ValueError("tool_name must be a non-empty string")
        return tuple(event for event in self.events if event.tool_name == tool_name)


"""
🤔 思考题：

1. 审计日志应该记录工具参数吗？如果参数里有密钥怎么办？
2. 为什么 by_tool 返回 tuple 而不是 list？
3. 如果日志文件写入失败，应该让工具调用失败还是只报警？
4. ⚡ 优化建议：未来可以增加 request_id/run_id，把一次 Agent 任务里的所有工具调用串起来。
"""
