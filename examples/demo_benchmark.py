"""
📦 模块名称：Benchmark Demo
📍 架构位置：示例层，演示 athena.evaluation 的最小可运行路径。
🎯 核心作用：展示如何用 BenchmarkEngine 跑用例，并用 BenchmarkReport 输出 Markdown 报告。
🔗 依赖关系：依赖 AgentResponse、BenchmarkCase、BenchmarkEngine、BenchmarkReport；供开发者手动运行学习。
💡 设计思路：使用 deterministic runner 假 Agent，避免评测 Demo 依赖外部模型导致结果不稳定。
📚 学习重点：理解 runner 注入为什么让评测系统和 Agent 实现解耦。
"""

from __future__ import annotations

import asyncio

from athena.agent.base import AgentResponse
from athena.evaluation import BenchmarkCase, BenchmarkEngine, BenchmarkReport


async def runner(query: str) -> AgentResponse:
    """
    Tiny deterministic runner for demo.

    功能说明：模拟一个永远成功的 Agent。
    参数说明：query 是 BenchmarkCase 输入的问题。
    返回值：AgentResponse。
    设计思路：Demo 用假 runner 可以稳定输出，方便学习 Benchmark 流程。
    使用示例：response = await runner("check pod")
    """
    return AgentResponse(
        answer=f"diagnosis ok: {query}", steps=["plan", "execute", "validate"]
    )


async def main() -> None:
    """
    Run benchmark demo.

    功能说明：创建评测引擎、运行一个用例、打印 Markdown 报告。
    参数说明：无。
    返回值：None。
    设计思路：把评测数据流压缩到最小例子：case → engine → results → report。
    使用示例：python examples/demo_benchmark.py
    """
    engine = BenchmarkEngine(runner)
    # 💡 学习提示：expected_keywords 是最简单的自动判分方式，答案里包含 diagnosis 就算成功。
    results = await engine.run_cases(
        (
            BenchmarkCase(
                name="basic", query="check pod", expected_keywords=("diagnosis",)
            ),
        )
    )
    print(BenchmarkReport.from_results(results).to_markdown())


if __name__ == "__main__":
    asyncio.run(main())


"""
🤔 思考题：

1. 如果 runner 返回的答案不包含 diagnosis，报告里的 Success Rate 会是多少？
2. 为什么 BenchmarkEngine 接收 runner，而不是直接创建 ReActAgent？
3. 如果要和 LangChain 方案对比，你只需要替换哪里？
"""
