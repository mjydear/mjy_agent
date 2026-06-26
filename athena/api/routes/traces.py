"""
📦 模块名称：执行轨迹 API 路由
📍 架构位置：HTTP 路由层，连接 Web 控制台右侧轨迹面板和任务记录。
🎯 核心作用：提供 GET /api/traces/{task_id}，查询单个任务的执行步骤。
🔗 依赖关系：依赖 TraceResponse 和 AthenaWebService；被 server.py 挂载。
💡 设计思路：轨迹查询按 task_id 精确读取，避免前端一次拉取所有任务细节。
📚 学习重点：理解 trace 是调试 Agent 的关键数据：它说明 Agent 为什么给出这个答案。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from athena.api.routes._deps import get_service
from athena.api.schemas import TraceResponse
from athena.api.services import AthenaWebService

router = APIRouter(prefix="/api/traces", tags=["traces"])


@router.get("/{task_id}", response_model=TraceResponse)
async def get_traces(
    task_id: str, service: AthenaWebService = Depends(get_service)
) -> TraceResponse:
    """
    查询任务轨迹。

    功能说明：按 task_id 返回任务执行过程中记录的 StepTrace 列表。
    参数说明：task_id 来自 URL；service 是注入的服务层。
    返回值：TraceResponse。
    设计思路：轨迹单独成接口，前端可以在用户切换详情面板时再加载，降低主流程负担。
    使用示例：GET /api/traces/chat-xxx
    """
    return service.get_traces(task_id)


"""
🤔 思考题：

1. 如果 trace 内容很大，接口是否需要分页或按 step_index 范围查询？
2. 轨迹里是否应该记录工具参数？如果参数包含敏感信息怎么办？
3. trace 和日志有什么区别？它们各自适合解决什么问题？
"""
