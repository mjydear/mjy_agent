"""
📦 模块名称：运行时指标（Runtime Metrics）
📍 架构位置：可观测性层，位于 Agent 执行流程旁路，用于记录运行表现。
🎯 核心作用：统计任务成功率、失败率、耗时和流式首事件延迟。
🔗 依赖关系：只依赖标准库 time/dataclass；被 observability web server 和未来监控面板读取。
💡 设计思路：用一个轻量内存指标对象先跑通 MVP，后续可替换为 Prometheus、OpenTelemetry 等后端。
📚 学习重点：理解指标系统不参与业务决策，但能帮助你判断 Agent 是否稳定、是否变慢。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class RuntimeMetrics:
    """
    运行时指标快照。

    功能说明：保存当前进程内的运行统计。
    参数说明：
        task_success：成功任务数。
        task_failure：失败任务数。
        stream_first_token_latencies：流式首事件延迟列表。
        task_durations：任务总耗时列表。
    返回值：数据容器；方法返回具体指标。
    设计思路：先把指标集中在一个对象里，Web 层只负责展示，不负责计算。
    使用示例：metrics.record_task(True, 0.3); metrics.success_rate()
    """

    task_success: int = 0
    task_failure: int = 0
    stream_first_token_latencies: list[float] = field(default_factory=list)
    task_durations: list[float] = field(default_factory=list)

    def record_task(self, success: bool, duration_seconds: float) -> None:
        """
        记录任务结果和耗时。

        功能说明：根据 success 增加成功/失败计数，并保存耗时。
        参数说明：success 表示任务是否成功；duration_seconds 是任务耗时秒数。
        返回值：None。
        设计思路：把计数和耗时写在一起，保证一次任务记录是完整的。
        使用示例：metrics.record_task(success=True, duration_seconds=1.2)
        """
        if duration_seconds < 0:
            raise ValueError("duration_seconds must be non-negative")
        if success:
            self.task_success += 1
        else:
            self.task_failure += 1
        self.task_durations.append(duration_seconds)

    def record_first_token(self, started_at: float) -> None:
        """
        记录流式首事件延迟。

        功能说明：计算从开始执行到第一个流式事件出现的耗时。
        参数说明：started_at 是 time.perf_counter() 记录的开始时间。
        返回值：None。
        设计思路：首事件延迟决定用户是否感觉“系统有响应”。
        使用示例：started = time.perf_counter(); metrics.record_first_token(started)
        """
        self.stream_first_token_latencies.append(
            max(0.0, time.perf_counter() - started_at)
        )  # 💡 学习提示：max 防止极端时钟误差导致负数指标。

    def success_rate(self) -> float:
        """
        返回任务成功率。

        功能说明：成功数 / 总任务数。
        参数说明：无。
        返回值：0 到 1 的成功率；没有任务时返回 0。
        设计思路：没有任务时不抛异常，方便监控页面冷启动展示。
        使用示例：rate = metrics.success_rate()
        """
        total = self.task_success + self.task_failure
        return self.task_success / total if total else 0.0


"""
🤔 思考题：

1. 没有任务时 success_rate 返回 0 合理吗？是否也可以返回 None？
2. 如果 Agent 长时间运行，task_durations 列表会越来越大，应该如何处理？
3. 首事件延迟和总耗时分别能反映什么用户体验问题？
4. ⚡ 优化建议：未来可以加入滑动窗口统计，避免内存无限增长。
"""
