"""
📦 模块名称：多 Agent 工作流 Demo
📍 架构位置：示例层，演示 athena.agent.workflow 的最小可运行路径。
🎯 核心作用：展示 Planner → Executor → Validator 如何协作完成一个多步骤任务。
🔗 依赖关系：依赖 WorkflowEngine、PlannerAgent、ExecutorAgent、ValidatorAgent；供开发者手动运行学习。
💡 设计思路：用固定字符串和确定性输出演示架构，不依赖外部 LLM 或 API Key。
📚 学习重点：看 main() 如何组装三个角色并执行一个任务。
"""

from __future__ import annotations

import asyncio

from athena.agent.workflow import (
    ExecutorAgent,
    PlannerAgent,
    ValidatorAgent,
    WorkflowEngine,
)


async def main() -> None:
    """
    Run the multi-agent demo.

    功能说明：创建工作流引擎并运行一个三步骤任务。
    参数说明：无。
    返回值：None，结果直接打印到终端。
    设计思路：Demo 保持最小化，帮助你先理解调用方式，再回头看源码细节。
    使用示例：python examples/demo_multi_agent.py
    """
    engine = WorkflowEngine(PlannerAgent(), ExecutorAgent(), ValidatorAgent())
    # 💡 学习提示：这里用分号显式拆步骤，是为了让 Planner 的规则规划结果一眼可见。
    response = await engine.run(
        "inspect service health; collect logs; validate recovery plan"
    )
    print(response.answer)


if __name__ == "__main__":
    asyncio.run(main())


"""
🤔 思考题：

1. 如果把任务字符串里的分号去掉，Planner 会生成几个步骤？
2. 如果要让 Demo 调用真实工具，需要给 ExecutorAgent 传入什么？
3. 为什么 Demo 不依赖真实 LLM？这对学习和测试有什么好处？
"""
