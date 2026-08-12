"""
📦 模块名称：Benchmark 报告生成器（Benchmark Report）
📍 架构位置：评测层的聚合输出组件，位于 BenchmarkResult 之后。
🎯 核心作用：把多个用例结果聚合成成功率、幻觉率、恢复率等摘要，并输出 Markdown。
🔗 依赖关系：依赖 BenchmarkResult；被 Demo、文档和未来 CI 报告调用。
💡 设计思路：原始结果和报告分离，方便同一批结果输出 Markdown、JSON 或 Web 图表。
📚 学习重点：看 from_results 如何从“多条明细”变成“一份摘要”。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from athena.evaluation.benchmark import BenchmarkResult


@dataclass(frozen=True)
class BenchmarkReport:
    """
    Benchmark 聚合报告。

    功能说明：保存评测总体指标。
    参数说明：
        total：用例总数。
        success_rate：成功率。
        hallucination_rate：幻觉率。
        recovery_rate：恢复率。
        average_steps：平均步骤数。
        average_duration_seconds：平均耗时。
    返回值：from_results() 返回 BenchmarkReport，to_markdown() 返回字符串。
    设计思路：报告对象既能作为数据结构，也能负责轻量渲染。
    使用示例：BenchmarkReport.from_results(results).to_markdown()
    """

    total: int
    success_rate: float
    hallucination_rate: float
    recovery_rate: float
    average_steps: float
    average_duration_seconds: float

    @classmethod
    def from_results(cls, results: Sequence[BenchmarkResult]) -> "BenchmarkReport":
        """
        从单项结果生成聚合报告。

        功能说明：把多条 BenchmarkResult 聚合成平均指标。
        参数说明：results 是单项结果序列。
        返回值：BenchmarkReport。
        设计思路：集中计算公式，避免 Demo、CI、Web 多处重复写聚合逻辑。
        使用示例：report = BenchmarkReport.from_results(results)
        """
        if not results:
            raise ValueError("results must not be empty")
        total = len(results)
        return cls(
            total=total,
            success_rate=sum(1 for result in results if result.success) / total,
            hallucination_rate=sum(1 for result in results if result.hallucination)
            / total,
            recovery_rate=sum(1 for result in results if result.recovery_success)
            / total,
            average_steps=sum(result.step_count for result in results) / total,
            average_duration_seconds=sum(result.duration_seconds for result in results)
            / total,
        )

    def to_markdown(self) -> str:
        """
        渲染 Markdown 报告。

        功能说明：把报告指标转换成适合展示和复制的 Markdown 文本。
        参数说明：无。
        返回值：Markdown 字符串。
        设计思路：Markdown 对 README、CI 日志、面试展示都友好。
        使用示例：print(report.to_markdown())
        """
        return (
            "# Athena Benchmark Report\n\n"
            f"- Total Cases: {self.total}\n"
            f"- Success Rate: {self.success_rate:.2%}\n"
            f"- Hallucination Rate: {self.hallucination_rate:.2%}\n"
            f"- Recovery Rate: {self.recovery_rate:.2%}\n"
            f"- Average Steps: {self.average_steps:.2f}\n"
            f"- Average Duration: {self.average_duration_seconds:.3f}s\n"
        )


"""
🤔 思考题：

1. 如果某些用例权重更高，from_results 应该如何改？
2. 报告渲染是否应该和数据对象拆成两个类？什么时候有必要？
3. 平均耗时容易被极端慢任务影响，是否应该加入 P95？
4. ⚡ 优化建议：未来可以提供 to_json()，方便 CI 系统机器读取。
"""
