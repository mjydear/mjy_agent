"""
📦 模块名称：多 Agent 工作流 API 路由
📍 架构位置：HTTP 路由层，连接浏览器工作流模式和 WorkflowEngine 编排能力。
🎯 核心作用：提供启动工作流和查询工作流状态两个接口。
🔗 依赖关系：依赖 Workflow 请求/响应模型和 AthenaWebService；被 server.py 挂载。
💡 设计思路：路由层只暴露“运行”和“状态查询”两个动作，把 Planner/Executor/Validator 细节藏在服务层。
📚 学习重点：看复杂任务如何通过一个 API 进入多 Agent Plan-and-Execute 流程。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from athena.api.routes._deps import get_service
from athena.api.schemas import (
    WorkflowRunRequest,
    WorkflowRunResponse,
    WorkflowStatusResponse,
)
from athena.api.services import AthenaWebService

router = APIRouter(prefix="/api/workflow", tags=["workflow"])


@router.post("/run", response_model=WorkflowRunResponse)
async def run_workflow(
    request: WorkflowRunRequest, service: AthenaWebService = Depends(get_service)
) -> WorkflowRunResponse:
    """
    启动多 Agent 工作流。

    功能说明：接收复杂任务文本，交给服务层运行 plan_execute 工作流。
    参数说明：request 包含 task 和 workflow_type；service 是注入的服务层。
    返回值：WorkflowRunResponse。
    设计思路：API 保留 workflow_type，未来可以扩展不同工作流，而不改路径。
    使用示例：POST /api/workflow/run {"task":"收集日志; 校验"}
    """
    return await service.run_workflow(
        task=request.task, workflow_type=request.workflow_type
    )


@router.get("/{task_id}/status", response_model=WorkflowStatusResponse)
async def get_workflow_status(
    task_id: str, service: AthenaWebService = Depends(get_service)
) -> WorkflowStatusResponse:
    """
    查询工作流任务状态。

    功能说明：按 task_id 返回当前状态、答案、步骤和错误信息。
    参数说明：task_id 来自 URL 路径；service 是注入的服务层。
    返回值：WorkflowStatusResponse。
    设计思路：即使当前工作流是同步跑完，保留状态查询接口也方便未来改成后台任务。
    使用示例：GET /api/workflow/workflow-xxx/status
    """
    return service.get_task_status(task_id)


"""
🤔 思考题：

1. 如果工作流运行时间很长，run_workflow 是否应该立即返回 running？
2. workflow_type 现在只支持 plan_execute，未来还能有哪些类型？
3. 工作流状态和普通 chat 状态是否应该共用同一个 `/api/tasks/{id}` 接口？
"""
