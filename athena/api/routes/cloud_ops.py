"""
📦 模块名称：云运维场景 API 路由
📍 架构位置：HTTP 路由层，连接 Web 控制台云运维模式和 AthenaWebService CloudOps 能力。
🎯 核心作用：提供云运维模式列表、同步执行、流式执行和知识库检索接口。
🔗 依赖关系：依赖 CloudOps Pydantic 模型与 AthenaWebService；被 server.py 挂载。
💡 设计思路：路由保持薄封装，所有场景判断和执行闭环都放在服务层。
📚 学习重点：看四个云场景如何复用同一个 `/api/cloud-ops/run` 入口。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from starlette.responses import StreamingResponse

from athena.api.routes._deps import get_service
from athena.api.schemas import CloudOpsMode, CloudOpsRequest, CloudOpsResponse
from athena.api.services import AthenaWebService

router = APIRouter(prefix="/api/cloud-ops", tags=["cloud-ops"])


@router.get("/modes", response_model=list[CloudOpsMode])
async def list_modes(
    service: AthenaWebService = Depends(get_service),
) -> list[CloudOpsMode]:
    """
    获取 Web 控制台可展示的云运维子模式。

    功能说明：返回 K8s、资源巡检、故障排查、成本优化四个入口的元数据。
    参数说明：service 是 FastAPI 依赖注入的 AthenaWebService 实例。
    返回值：CloudOpsMode 列表，前端用它理解后端支持哪些模式。
    设计思路：模式列表由后端提供，避免前端写死能力清单后和后端实现不一致。
    使用示例：GET /api/cloud-ops/modes
    """
    return service.list_cloud_ops_modes()


@router.post("/run", response_model=CloudOpsResponse)
async def run_cloud_ops(
    request: CloudOpsRequest, service: AthenaWebService = Depends(get_service)
) -> CloudOpsResponse:
    """
    同步运行一个云运维场景。

    功能说明：接收 mode/task/provider/confirmed，转交服务层执行对应 CloudOps 闭环。
    参数说明：request 是请求体；service 是共享服务层对象。
    返回值：CloudOpsResponse，包含答案、轨迹、结构化数据和是否需要人工确认。
    设计思路：四个场景共用一个入口，像一个“场景路由器”，真正分发逻辑放在服务层。
    使用示例：POST /api/cloud-ops/run {"mode":"k8s","task":"巡检集群"}

    🎯 面试考点：为什么路由不直接调用 K8sDiagnoser？答案：路由层应保持薄，只负责 HTTP 边界，业务编排放在 service 更容易测试和复用。
    """
    return await service.run_cloud_ops(
        request.mode, request.task, request.provider, request.confirmed
    )


@router.post("/stream")
async def stream_cloud_ops(
    request: CloudOpsRequest, service: AthenaWebService = Depends(get_service)
) -> StreamingResponse:
    """
    以 SSE 形式流式运行云运维场景。

    功能说明：把服务层生成的 CloudOps 事件持续推给浏览器。
    参数说明：request 是云场景请求；service 是注入的业务服务。
    返回值：StreamingResponse，Content-Type 为 text/event-stream。
    设计思路：复用聊天流式接口同一种 SSE 协议，前端解析逻辑可以尽量统一。
    使用示例：POST /api/cloud-ops/stream {"mode":"fault"}
    """
    return StreamingResponse(
        service.stream_cloud_ops(
            request.mode, request.task, request.provider, request.confirmed
        ),
        media_type="text/event-stream",
    )


@router.get("/knowledge")
async def search_knowledge(
    query: str = Query(default=""), service: AthenaWebService = Depends(get_service)
) -> dict[str, object]:
    """
    检索云运维知识库。

    功能说明：按关键字搜索故障排查流程沉淀出的运维案例。
    参数说明：query 是检索词；service 是共享服务层。
    返回值：包含 query 和 items 的字典，items 是匹配到的知识项。
    设计思路：知识库查询是只读动作，用 GET 更贴近资源查询语义。
    使用示例：GET /api/cloud-ops/knowledge?query=CrashLoop
    """
    return service.search_ops_knowledge(query)


"""
🤔 思考题：

1. 如果四个模式以后参数差异很大，是继续共用 CloudOpsRequest，还是拆成多个请求模型？
2. 为什么知识库检索单独做 GET 接口，而不是塞进 run？
3. 如果要加人工确认弹窗，前端应该根据哪个字段判断？
"""
