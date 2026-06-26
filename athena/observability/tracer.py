"""
📦 模块名称：流式追踪收集器（Streaming Trace Collector）
📍 架构位置：可观测性层，连接 ReActAgent.stream_run() 和 Tracer。
🎯 核心作用：把 StreamEvent 转换成统一 TraceEvent，方便存储、回放和展示。
🔗 依赖关系：依赖 StreamEvent、TraceEvent、Tracer；被流式 UI 或 Web 服务调用。
💡 设计思路：使用转换器模式，让流式事件和通用追踪事件保持解耦。
📚 学习重点：理解为什么事件模型要统一，统一后日志、调试和评测都能复用。
"""

from __future__ import annotations

from athena.agent.executor import StreamEvent
from athena.learning.tracer import TraceEvent, Tracer


class StreamingTraceCollector:
    """
    把 StreamEvent 转换为 TraceEvent 的链路追踪收集器。

    功能说明：接收流式事件，并写入 Tracer。
    参数说明：tracer 是底层追踪存储器。
    返回值：record_stream_event() 返回 None。
    设计思路：Agent 只负责产出 StreamEvent，Collector 负责落到追踪体系，各自职责清晰。
    使用示例：collector.record_stream_event("run-1", event)
    """

    def __init__(self, tracer: Tracer) -> None:
        self.tracer = tracer

    def record_stream_event(self, run_id: str, event: StreamEvent) -> None:
        """
        记录一个流式事件。

        功能说明：把 event_type、content、step_index 转换成 TraceEvent payload。
        参数说明：run_id 是运行 id；event 是流式事件。
        返回值：None。
        设计思路：TraceEvent.name 使用 stream.xxx，方便按事件类型筛选。
        使用示例：collector.record_stream_event("run-1", StreamEvent("final", "ok"))
        """
        if not run_id.strip():
            raise ValueError("run_id must be non-empty")
        self.tracer.record(
            TraceEvent(
                name=f"stream.{event.event_type}",
                run_id=run_id,
                payload={
                    "content": event.content,
                    "step_index": str(event.step_index),
                },  # 💡 学习提示：payload 统一为 JSON 友好值，便于未来写入文件或 Web API。
            )
        )


"""
🤔 思考题：

1. step_index 为什么要放进 payload，而不是 TraceEvent 顶层字段？
2. 如果 content 很大，直接记录完整内容会有什么问题？
3. 如果要支持 WebSocket 推送，Collector 应该负责推送吗？
4. ⚡ 优化建议：未来可以给 StreamEvent 增加 timestamp，减少转换时的信息损失。
"""
