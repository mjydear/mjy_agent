"""
📦 模块名称：对话 API 路由
📍 架构位置：HTTP 路由层，位于 FastAPI app 和 AthenaWebService 之间。
🎯 核心作用：暴露同步对话和 SSE 流式对话两个 HTTP 入口。
🔗 依赖关系：依赖 ChatRequest/ChatResponse 和 AthenaWebService；被 server.py 挂载。
💡 设计思路：路由保持“薄”，只做参数接收和响应包装，真正业务逻辑交给 service。
📚 学习重点：看 Depends(get_service) 如何把共享服务注入到每个接口里。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from starlette.responses import StreamingResponse

from athena.api.routes._deps import get_service
from athena.api.schemas import ChatRequest, ChatResponse
from athena.api.services import AthenaWebService

router = APIRouter(
    prefix="/api/chat", tags=["chat"]
)  # 💡 学习提示：prefix 统一加前缀，下面的路径就能保持简短。


@router.post("", response_model=ChatResponse)
async def chat(
    request: ChatRequest, service: AthenaWebService = Depends(get_service)
) -> ChatResponse:
    """
    执行同步对话。

    功能说明：接收 session_id 和 message，等待 Agent 完整执行后一次性返回答案。
    参数说明：request 是请求体；service 是 FastAPI 依赖注入进来的服务层对象。
    返回值：ChatResponse。
    设计思路：路由不直接调用 Agent，是为了让 HTTP 层和业务层解耦，测试也更容易。
    使用示例：POST /api/chat {"session_id":"s1","message":"hello"}
    """
    return await service.chat(request.session_id, request.message)


@router.post("/stream")
async def stream_chat(
    request: ChatRequest, service: AthenaWebService = Depends(get_service)
) -> StreamingResponse:
    """
    执行 SSE 流式对话。

    功能说明：把 service.stream_chat() 产生的异步文本块持续推送给浏览器。
    参数说明：request 是请求体；service 是共享服务层对象。
    返回值：StreamingResponse，浏览器会按流读取。
    设计思路：SSE 比 WebSocket 更轻，适合服务器单向推送 Agent 事件。
    使用示例：POST /api/chat/stream {"session_id":"s1","message":"hello"}

    🎯 面试考点：为什么 media_type 是 text/event-stream？答案：这是浏览器识别 SSE 流的标准 Content-Type。
    """
    return StreamingResponse(
        service.stream_chat(request.session_id, request.message),
        media_type="text/event-stream",
    )


"""
🤔 思考题：

1. 如果要支持用户中途取消后端任务，路由层需要知道取消逻辑吗？
2. 为什么同步和流式接口共用 ChatRequest？
3. 如果要改成 WebSocket，service.stream_chat() 能复用多少？
"""
