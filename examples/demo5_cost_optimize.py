"""
📦 模块名称：Demo5 - 云资源成本优化演示
📍 架构位置：示例层，位于用户手动运行入口和 AthenaWebService CloudOps 成本模式之间。
🎯 核心作用：演示如何通过服务层运行成本优化并输出闲置资源报告。
🔗 依赖关系：依赖 AthenaWebService；不依赖真实 LLM 或真实云账号。
💡 设计思路：复用 Web API 服务层的 run_cloud_ops，证明 Demo、API、前端走同一套业务能力。
📚 学习重点：看为什么 CloudOps 示例可以不创建真实 ReActAgent 会话。
"""

from __future__ import annotations

import asyncio

from athena.agent import ReActAgent
from athena.api.services import AthenaWebService


def unused_agent_factory() -> ReActAgent:
    """
    Demo 专用的占位 Agent 工厂。

    功能说明：如果有人误触发会话创建，就主动报错。
    参数说明：无。
    返回值：理论上返回 ReActAgent，但本 Demo 不应该调用它。
    设计思路：CloudOps 成本分析不需要聊天 Agent；显式报错比悄悄创建假 Agent 更容易发现误用。
    使用示例：AthenaWebService(agent_factory=unused_agent_factory)
    """
    raise RuntimeError("demo5 does not create ReActAgent sessions")


async def main() -> None:
    """
    运行成本优化演示。

    功能说明：通过服务层触发 cost 模式，打印自然语言答案和结构化 data。
    参数说明：无。
    返回值：None。
    设计思路：直接走 AthenaWebService，可以验证 Web API 背后的真实业务入口。
    使用示例：python examples/demo5_cost_optimize.py
    """
    service = AthenaWebService(
        agent_factory=unused_agent_factory
    )  # 💡 学习提示：这里不创建 session，所以不会真正调用 unused_agent_factory。
    response = await service.run_cloud_ops("cost", provider="aliyun")
    print("# Cost Optimization Demo")
    print(response.answer)
    print(response.data)


if __name__ == "__main__":
    asyncio.run(main())


"""
🤔 思考题：

1. 为什么这个 Demo 选择调用服务层，而不是直接调用 AliyunClient？
2. 如果 provider 改成 aws，当前输出会有什么变化？
3. 成本节省金额现在是固定估算，真实系统应该接哪些数据源？
4. ⚡ 优化建议：未来可以把 response.data 渲染成 Markdown 表格，更适合面试展示。
"""
