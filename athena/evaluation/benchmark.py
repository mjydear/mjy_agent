"""
📦 模块名称：Agent Benchmark 引擎（Benchmark Engine）
📍 架构位置：评测层，位于 Agent Runner 外侧，用来批量运行测试用例并生成原始指标。
🎯 核心作用：用可复现用例衡量 Agent 的成功率、幻觉率、恢复率、步骤数和耗时。
🔗 依赖关系：依赖 AgentResponse；被 BenchmarkReport、Demo、CI 评测任务调用。
💡 设计思路：使用依赖注入传入 runner，这样 Benchmark 不绑定具体 Agent，测试时可以传 fake runner。
📚 学习重点：理解“评测系统要独立于被评测对象”，否则很难做公平比较。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from athena.agent.base import AgentResponse

AgentRunner = Callable[
    [str], Awaitable[AgentResponse]
]  # 💡 学习提示：类型别名让“异步 Agent 调用函数”这个概念更清楚。


@dataclass(frozen=True)
class BenchmarkCase:
    """
    单个运维场景评测用例。

    功能说明：描述一个可复现的 Agent 测试任务。
    参数说明：
        name：用例名称。
        query：输入给 Agent 的问题。
        expected_keywords：期望答案里包含的关键词。
        category：用例类别，例如 normal、exception。
    返回值：数据容器。
    设计思路：关键词匹配是第一版简单评测方式，可解释、可测试。
    使用示例：BenchmarkCase("basic", "check pod", ("ok",))
    """

    name: str
    query: str
    expected_keywords: tuple[str, ...]
    category: str = "normal"

    def __post_init__(self) -> None:
        if not self.name.strip():
            raise ValueError("case name must be non-empty")
        if not self.query.strip():
            raise ValueError("case query must be non-empty")


@dataclass(frozen=True)
class BenchmarkResult:
    """
    单个用例评测结果。

    功能说明：保存一个测试用例的执行指标。
    参数说明：
        case_name：用例名称。
        success：是否命中期望关键词。
        hallucination：是否疑似幻觉。
        recovery_success：异常类任务是否恢复成功。
        step_count：Agent 使用步骤数。
        duration_seconds：耗时秒数。
    返回值：数据容器。
    设计思路：保留原子结果，再由 Report 做聚合，方便未来导出 JSON/CSV。
    使用示例：BenchmarkResult("case", True, False, True, 3, 0.2)
    """

    case_name: str
    success: bool
    hallucination: bool
    recovery_success: bool
    step_count: int
    duration_seconds: float


class BenchmarkEngine:
    """
    批量 Benchmark 执行引擎。

    功能说明：批量运行 BenchmarkCase，并收集 BenchmarkResult。
    参数说明：runner 是一个异步函数，输入 query，输出 AgentResponse。
    返回值：run_cases() 返回 BenchmarkResult 列表。
    设计思路：Benchmark 只负责“怎么测”，不负责“Agent 怎么实现”。
    使用示例：engine = BenchmarkEngine(agent.run); results = await engine.run_cases(cases)

    🎯 面试考点：为什么 runner 用依赖注入？答案：可以同时评测单 Agent、多 Agent、假 Agent，而不改评测代码。
    """

    def __init__(self, runner: AgentRunner) -> None:
        self.runner = runner

    async def run_cases(self, cases: Sequence[BenchmarkCase]) -> list[BenchmarkResult]:
        """
        批量执行测试用例并收集指标。

        功能说明：逐个调用 runner，计算成功、幻觉、恢复、步骤数和耗时。
        参数说明：cases 是评测用例序列。
        返回值：BenchmarkResult 列表。
        设计思路：逐个执行而不是并发执行，第一版保证结果稳定；未来可以加并发评测。
        使用示例：results = await engine.run_cases((case1, case2))

        🔍 原理讲解：
        输入：多个 BenchmarkCase。
        处理过程：记录开始时间 → 调用 Agent → 检查关键词 → 判断幻觉 → 汇总指标。
        输出：每个用例一个 BenchmarkResult。
        """
        if not cases:
            raise ValueError("cases must not be empty")
        results: list[BenchmarkResult] = []
        for case in cases:
            started_at = (
                time.perf_counter()
            )  # 💡 学习提示：perf_counter 适合测耗时，比 time.time 更适合性能统计。
            response = await self.runner(case.query)
            duration = time.perf_counter() - started_at
            success = all(
                keyword.lower() in response.answer.lower()
                for keyword in case.expected_keywords
            )  # 💡 学习提示：关键词法简单但粗糙，适合 MVP，不适合最终语义评测。
            hallucination = self._detect_hallucination(response, case)
            recovery_success = case.category != "exception" or success
            results.append(
                BenchmarkResult(
                    case_name=case.name,
                    success=success,
                    hallucination=hallucination,
                    recovery_success=recovery_success,
                    step_count=len(response.steps),
                    duration_seconds=duration,
                )
            )
        return results

    def _detect_hallucination(
        self, response: AgentResponse, case: BenchmarkCase
    ) -> bool:
        """
        检测疑似幻觉。

        功能说明：用简单规则判断答案是否为空或出现 unknown。
        参数说明：response 是 Agent 输出；case 是对应用例。
        返回值：布尔值，True 表示疑似幻觉。
        设计思路：先用可解释规则建立指标管道，未来再升级为事实校验或 LLM judge。
        使用示例：engine._detect_hallucination(response, case)
        """
        if not response.answer.strip():
            return True
        if "unknown" in response.answer.lower() and case.expected_keywords:
            return True
        return False


"""
🤔 思考题：

1. 关键词匹配有哪些误判场景？
2. 如果要评测“回答质量”，除了 success_rate 还应该加什么指标？
3. 为什么第一版不并发运行所有 cases？
4. ⚡ 优化建议：未来可以支持 case.expected_regex、golden answer 和人工评分。
"""
