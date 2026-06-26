"""
📦 模块名称：会话管理 API 路由
📍 架构位置：HTTP 路由层，连接浏览器会话列表和 AthenaWebService 会话管理能力。
🎯 核心作用：提供创建会话、列出会话、查看会话详情三个接口。
🔗 依赖关系：依赖 Session 模型和 AthenaWebService；被 server.py 统一挂载。
💡 设计思路：保持 REST 风格：集合路径 `/api/sessions` 管创建和列表，带 id 的路径管单个详情。
📚 学习重点：理解 session 是 Web 层隔离 Agent 记忆上下文的最小单位。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from athena.api.routes._deps import get_service
from athena.api.schemas import (
    SessionCreateRequest,
    SessionCreateResponse,
    SessionDetail,
    SessionSummary,
)
from athena.api.services import AthenaWebService

router = APIRouter(prefix="/api/sessions", tags=["sessions"])


@router.post("", response_model=SessionCreateResponse)
async def create_session(
    request: SessionCreateRequest, service: AthenaWebService = Depends(get_service)
) -> SessionCreateResponse:
    """
    创建独立 Web 会话。

    功能说明：调用服务层创建一个新 Agent 会话并返回详情。
    参数说明：request 包含可选 title；service 是注入的服务层。
    返回值：SessionCreateResponse。
    设计思路：返回完整详情而不是只返回 id，前端创建后可以立即展示当前会话。
    使用示例：POST /api/sessions {"title":"demo"}
    """
    return SessionCreateResponse(session=service.create_session(request.title))


@router.get("", response_model=list[SessionSummary])
async def list_sessions(
    service: AthenaWebService = Depends(get_service),
) -> list[SessionSummary]:
    """
    获取会话列表。

    功能说明：返回所有活跃会话摘要，供左侧边栏渲染。
    参数说明：service 是注入的服务层。
    返回值：SessionSummary 列表。
    设计思路：列表接口返回摘要，避免每次刷新侧边栏都传完整消息历史。
    使用示例：GET /api/sessions
    """
    return service.list_sessions()


@router.get("/{session_id}", response_model=SessionDetail)
async def get_session(
    session_id: str, service: AthenaWebService = Depends(get_service)
) -> SessionDetail:
    """
    获取单个会话详情。

    功能说明：按 session_id 返回会话元信息和消息历史。
    参数说明：session_id 来自 URL 路径；service 是注入的服务层。
    返回值：SessionDetail。
    设计思路：详情接口只在用户切换会话时调用，数据量更合理。
    使用示例：GET /api/sessions/session-xxx
    """
    return service.get_session(session_id)


"""
🤔 思考题：

1. 如果要删除会话，你会新增哪个 HTTP 方法和路径？
2. 会话标题现在只能创建时传入，如果要重命名，应该加哪个接口？
3. 会话历史如果很多，get_session 是否需要分页？
"""
