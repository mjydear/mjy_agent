"""
📦 模块名称：Benchmark API 路由
📍 架构位置：HTTP 路由层，连接 Web 控制台 Benchmark Tab 和评测服务能力。
🎯 核心作用：提供启动 Benchmark 和查询 Benchmark 报告两个接口。
🔗 依赖关系：依赖 Benchmark 请求/响应模型和 AthenaWebService；被 server.py 挂载。
💡 设计思路：把“运行评测”和“读取报告”拆成两个接口，贴近真实评测系统的生命周期。
📚 学习重点：理解 Benchmark 是评估 Agent 表现的独立能力，不应该混在普通对话接口里。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from athena.api.routes._deps import get_service
from athena.api.schemas import (
    BenchmarkReportResponse,
    BenchmarkRunRequest,
    BenchmarkRunResponse,
)
from athena.api.services import AthenaWebService

router = APIRouter(prefix="/api/benchmark", tags=["benchmark"])


@router.post("/run", response_model=BenchmarkRunResponse)
async def run_benchmark(
    request: BenchmarkRunRequest, service: AthenaWebService = Depends(get_service)
) -> BenchmarkRunResponse:
    """
    启动 Benchmark 评测。

    功能说明：按 case_set 运行一组评测用例，并把报告保存到服务层内存中。
    参数说明：request 包含 case_set；service 是注入的服务层。
    返回值：BenchmarkRunResponse。
    设计思路：路由只转发 case_set，不关心评测如何打分，这样 BenchmarkEngine 可以独立演进。
    使用示例：POST /api/benchmark/run {"case_set":"smoke"}
    """
    return await service.run_benchmark(request.case_set)


@router.get("/{run_id}/report", response_model=BenchmarkReportResponse)
async def get_benchmark_report(
    run_id: str, service: AthenaWebService = Depends(get_service)
) -> BenchmarkReportResponse:
    """
    查询 Benchmark 报告。

    功能说明：根据 run_id 返回之前保存的 Markdown 报告。
    参数说明：run_id 来自 URL；service 是注入的服务层。
    返回值：BenchmarkReportResponse。
    设计思路：报告查询独立出来，未来评测变成后台异步任务时前端仍可复用这个接口。
    使用示例：GET /api/benchmark/benchmark-xxx/report
    """
    return service.get_benchmark_report(run_id)


"""
🤔 思考题：

1. 如果 Benchmark 运行时间很长，run_benchmark 应该立即返回 run_id 还是等待完成？
2. 报告现在存在内存里，服务重启后会怎样？
3. 如果要支持多套评测用例文件，case_set 应该映射到什么位置？
"""
