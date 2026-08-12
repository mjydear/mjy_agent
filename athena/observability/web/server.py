"""
📦 模块名称：可观测 Web 服务（Observability Web Server）
📍 架构位置：可观测性展示层，位于 Tracer/RuntimeMetrics 之上。
🎯 核心作用：提供 HTTP API 查看 trace 和 runtime metrics。
🔗 依赖关系：依赖 FastAPI、Tracer、RuntimeMetrics；可被 uvicorn 启动或测试直接创建 app。
💡 设计思路：Web 层保持很薄，只负责把内存状态暴露成接口，不掺杂业务逻辑。
📚 学习重点：理解 create_app 工厂函数如何让测试和生产启动都更灵活。
"""

from __future__ import annotations

from athena.learning.tracer import Tracer
from athena.observability.metrics import RuntimeMetrics


def create_app(tracer: Tracer | None = None, metrics: RuntimeMetrics | None = None):
    """
    创建可视化管理界面 FastAPI app。

    功能说明：创建 FastAPI 实例，并注册 /traces 和 /metrics 接口。
    参数说明：
        tracer：可选追踪存储器，不传则创建新的 Tracer。
        metrics：可选指标存储器，不传则创建新的 RuntimeMetrics。
    返回值：FastAPI app 实例。
    设计思路：采用 app factory 模式，避免导入模块时就启动服务，也方便测试注入假数据。
    使用示例：uvicorn.run(create_app(), host="127.0.0.1", port=8000)

    🎯 面试考点：为什么在函数内部 import FastAPI？答案：让没有安装 fastapi 的环境仍可导入其他 observability 模块。
    """
    try:
        from fastapi import FastAPI
    except ImportError as exc:
        raise RuntimeError(
            "fastapi is required for the observability web server"
        ) from exc

    app = FastAPI(
        title="Athena Observability"
    )  # 💡 学习提示：这里不直接启动服务器，只创建 app，启动交给 uvicorn 或外部命令。
    trace_store = tracer or Tracer()
    metric_store = metrics or RuntimeMetrics()

    @app.get("/traces")
    def traces() -> list[dict[str, object]]:
        """返回当前追踪事件列表，供前端时间线展示。"""
        return [
            {
                "name": event.name,
                "run_id": event.run_id,
                "timestamp": event.timestamp,
                "payload": event.payload,
            }
            for event in trace_store.events
        ]

    @app.get("/metrics")
    def runtime_metrics() -> dict[str, object]:
        """返回运行时指标快照，供仪表盘展示。"""
        return {
            "success_rate": metric_store.success_rate(),
            "task_success": metric_store.task_success,
            "task_failure": metric_store.task_failure,
            "task_durations": metric_store.task_durations,
            "first_token_latencies": metric_store.stream_first_token_latencies,
        }

    return app


"""
🤔 思考题：

1. 如果 trace 很多，/traces 一次返回全部会有什么性能问题？
2. 为什么 Web 层不直接修改 metrics，而只读取？
3. 如果要做实时页面，应该用轮询、SSE 还是 WebSocket？
4. ⚡ 优化建议：未来可以给 /traces 增加 run_id 查询参数和分页。
"""
