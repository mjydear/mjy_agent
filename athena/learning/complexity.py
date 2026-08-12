"""
📦 模块名称：执行复杂度评估（Execution Complexity）
📍 架构位置：GEPA 自进化闭环的第一步：
              [TraceEvent] → 【ComplexityEvaluator】 → [SkillGenerator]
🎯 核心作用：把一次任务轨迹量化为复杂度分数，决定是否值得沉淀为 Skill。
🔗 依赖关系：依赖 athena.learning.tracer.TraceEvent；被 SkillGenerator、Curator 或未来自进化调度器调用。
💡 设计思路：
    复杂度不是只看步数，而是综合“步骤数、工具多样性、问题难度系数”。
    这样可以避免把简单 echo 任务误判为高价值经验。
    这里使用策略对象 ComplexityWeights，让评分权重以后可以独立调整。
📚 学习重点：看懂“把行为轨迹转成数字分数”的过程，这是 Agent 自我改进的第一道筛选门。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from athena.learning.tracer import TraceEvent


@dataclass(frozen=True)
class ComplexityWeights:
    """
    复杂度评分权重。

    功能说明：定义步骤数、工具多样性、任务难度三类因素分别占多少比例。
    参数说明：
        steps：执行步骤越多，通常说明任务越复杂。
        tool_diversity：用到的工具越多，通常说明任务跨越了更多能力边界。
        difficulty：外部传入的主观难度，例如用户或评测集标注。
    返回值：这是配置数据类，不直接返回业务结果。
    设计思路：把权重抽出来，避免把魔法数字散落在评分逻辑里。
    使用示例：ComplexityWeights(steps=0.5, tool_diversity=0.3, difficulty=0.2)

    🎯 面试考点：为什么用 dataclass？答案：它适合表达“只有字段和少量校验”的配置对象，代码比普通类更清晰。
    """

    steps: float = 0.4
    tool_diversity: float = 0.35
    difficulty: float = 0.25

    def __post_init__(self) -> None:
        """
        校验权重是否合法。

        功能说明：防止负权重或全 0 权重导致评分没有意义。
        参数说明：无，dataclass 会在初始化后自动调用。
        返回值：None。
        设计思路：在对象创建时就阻止错误配置，后面的 evaluate 就不用重复兜底。
        使用示例：ComplexityWeights(-1, 0.3, 0.2) 会抛出 ValueError。
        """
        values = (self.steps, self.tool_diversity, self.difficulty)
        if any(value < 0 for value in values):
            raise ValueError("complexity weights must be non-negative")
        if sum(values) <= 0:
            raise ValueError("at least one complexity weight must be positive")


@dataclass(frozen=True)
class ComplexityScore:
    """
    一次执行轨迹的复杂度评分详情。

    功能说明：保存最终分数和分数背后的原始统计信息。
    参数说明：
        score：0 到 1 之间的综合复杂度分。
        step_count：轨迹里识别出的步骤数量。
        tool_count：轨迹里使用过的不同工具数量。
        difficulty：外部传入的任务难度。
        should_generate_skill：是否建议进入 Skill 生成阶段。
    返回值：数据容器，不直接执行逻辑。
    设计思路：不要只返回一个 float，因为调试时需要知道分数为什么高或低。
    使用示例：score.should_generate_skill 判断是否进入下一步。
    """

    score: float
    step_count: int
    tool_count: int
    difficulty: float
    should_generate_skill: bool


class ComplexityEvaluator:
    """
    GEPA 复杂度评估器。

    功能说明：读取 TraceEvent 列表，计算这次任务是否值得沉淀为 Skill。
    参数说明：
        weights：复杂度权重配置，不传则使用默认权重。
        skill_threshold：超过该阈值才建议生成 Skill。
    返回值：构造函数无返回；evaluate() 返回 ComplexityScore。
    设计思路：这是一个“规则评分器”，先用可解释规则跑通闭环，未来可以替换成模型评分。
    使用示例：ComplexityEvaluator(skill_threshold=0.6).evaluate(events)
    """

    def __init__(
        self, weights: ComplexityWeights | None = None, skill_threshold: float = 0.55
    ) -> None:
        if skill_threshold < 0 or skill_threshold > 1:
            raise ValueError("skill_threshold must be in range 0..1")
        self.weights = weights or ComplexityWeights()
        self.skill_threshold = skill_threshold

    def evaluate(
        self, events: Sequence[TraceEvent], task_difficulty: float = 0.5
    ) -> ComplexityScore:
        """
        根据轨迹事件计算复杂度。

        功能说明：统计步骤数和工具种类，结合任务难度生成综合分。
        参数说明：
            events：一次 Agent 运行过程中记录的事件列表。
            task_difficulty：任务难度，范围 0..1。
        返回值：ComplexityScore。
        设计思路：先归一化不同量纲的数据，再按权重合成一个可比较的分数。
        使用示例：score = evaluator.evaluate(events, task_difficulty=0.8)

        🔍 原理讲解：
        不同指标单位不同，不能直接相加。步骤数可能是 6，工具数可能是 2，难度是 0.8。
        所以先把步骤数除以 8、工具数除以 4，压到 0..1，再做加权平均。

        举个例子：
        输入 4 个步骤 + 2 个工具 + 难度 0.8 → 归一化为 0.5、0.5、0.8 → 输出一个综合复杂度分。
        """
        if not events:
            return ComplexityScore(
                score=0.0,
                step_count=0,
                tool_count=0,
                difficulty=0.0,
                should_generate_skill=False,
            )
        if task_difficulty < 0 or task_difficulty > 1:
            raise ValueError("task_difficulty must be in range 0..1")

        # 💡 学习提示：这里允许 name.endswith("step")，是为了兼容未来可能出现的 workflow.step、planner.step 等事件名。
        step_count = sum(
            1
            for event in events
            if event.name.endswith("step") or event.name == "agent.step"
        )
        tool_names = {
            event.payload.get("tool", "")
            for event in events
            if event.name.startswith("tool") and event.payload.get("tool", "")
        }
        normalized_steps = min(
            step_count / 8.0, 1.0
        )  # 💡 学习提示：封顶到 1.0，避免超长任务把评分无限拉高。
        normalized_tools = min(
            len(tool_names) / 4.0, 1.0
        )  # 💡 学习提示：工具超过 4 种后边际收益降低，防止“乱用工具”被奖励。
        total_weight = (
            self.weights.steps + self.weights.tool_diversity + self.weights.difficulty
        )
        score = (
            normalized_steps * self.weights.steps
            + normalized_tools * self.weights.tool_diversity
            + task_difficulty * self.weights.difficulty
        ) / total_weight
        return ComplexityScore(
            score=score,
            step_count=step_count,
            tool_count=len(tool_names),
            difficulty=task_difficulty,
            should_generate_skill=score >= self.skill_threshold,
        )


"""
🤔 思考题：

1. 如果一个任务步骤很多但全是重复尝试，它真的应该生成 Skill 吗？你会加什么指标过滤？
2. 这里为什么先做归一化，而不是直接把 step_count 和 tool_count 相加？
3. 如果未来有失败轨迹，复杂度高但结果失败，应该进入 SkillGenerator 吗？
4. ⚡ 优化建议：可以加入 success、重试次数、工具失败率等指标，让复杂度不只看“做了多少”，也看“做得好不好”。
"""
