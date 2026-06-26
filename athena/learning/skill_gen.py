"""
📦 模块名称：Skill 自动生成（Skill Generator）
📍 架构位置：GEPA 自进化闭环中段：
              [TraceEvent + ComplexityScore] → 【SkillGenerator】 → [Skill]
🎯 核心作用：从成功执行轨迹中提取稳定模式，生成可入库、可检索、可复用的标准化 Skill。
🔗 依赖关系：依赖 ComplexityScore、TraceEvent 和 memory.skill.Skill；被 GEPA 学习闭环和未来 Curator 调用。
💡 设计思路：
    当前实现不依赖外部 Agent 框架，也不强依赖 LLM；先用结构化规则把轨迹转成 Skill。
    未来可把 build_skill() 的生成策略替换为 LLM 结构化 Prompt，但输出仍是同一个 Skill 对象。
📚 学习重点：重点看“抽取工具、抽取步骤、渲染内容、计算置信度”这条流水线。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from athena.learning.complexity import ComplexityScore
from athena.learning.tracer import TraceEvent
from athena.memory.skill import Skill


@dataclass(frozen=True)
class SkillGenerationResult:
    """
    Skill 生成结果。

    功能说明：保存生成出来的 Skill、来源 run_id 和生成置信度。
    参数说明：
        skill：最终生成的标准 Skill 对象。
        source_run_id：这个 Skill 来自哪一次执行轨迹。
        confidence：生成质量的置信度。
    返回值：数据容器，不主动返回。
    设计思路：不要只返回 Skill，因为追溯来源和质量分数对自进化系统很重要。
    使用示例：result = generator.build_skill("Inspect Logs", events, complexity, success=True)
    """

    skill: Skill
    source_run_id: str
    confidence: float


class SkillGenerator:
    """
    从执行轨迹生成标准化 Skill。

    功能说明：把 TraceEvent 列表转成可保存、可检索、可复用的 Skill。
    参数说明：无构造参数。
    返回值：build_skill() 返回 SkillGenerationResult。
    设计思路：采用“模板生成”方式，先保证输出稳定可测，再逐步引入 LLM 优化内容表达。
    使用示例：SkillGenerator().build_skill("Fix Pod", events, complexity, True)

    🎯 面试考点：为什么第一版不用 LLM 直接总结？答案：规则生成确定性强、测试稳定，适合 MVP 先跑通闭环。
    """

    def build_skill(
        self,
        name: str,
        events: Sequence[TraceEvent],
        complexity: ComplexityScore,
        success: bool,
    ) -> SkillGenerationResult:
        """
        生成一个 Skill，并给出置信度。

        功能说明：从执行事件中抽取工具和步骤，渲染成 Skill.content，并计算置信度。
        参数说明：
            name：候选 Skill 名称。
            events：来源执行轨迹。
            complexity：复杂度评分结果。
            success：这次任务是否成功。
        返回值：SkillGenerationResult。
        设计思路：把生成拆成多个小函数，便于以后单独替换“提取步骤”或“渲染模板”。
        使用示例：generated = generator.build_skill("Inspect Logs", events, score, success=True)

        🔍 原理讲解：
        输入：一次成功执行的 TraceEvent 列表。
        处理过程：提取工具 → 提取步骤 → 生成 Markdown 风格内容 → 计算置信度。
        输出：一个标准 Skill 对象，后续可交给 SkillValidator 验证。
        """
        if not isinstance(name, str) or not name.strip():
            raise ValueError("name must be a non-empty string")
        if not events:
            raise ValueError("events must not be empty")
        run_id = events[
            0
        ].run_id  # 💡 学习提示：默认同一批 events 来自同一次运行，所以用第一条事件保存来源。
        tools = self._extract_tools(events)
        steps = self._extract_steps(events)
        normalized_name = self._normalize_name(name)
        content = self._render_skill_content(steps, tools)
        confidence = self._score_confidence(success, complexity, tools)
        skill = Skill(
            name=normalized_name,
            description=f"Reusable workflow learned from run {run_id}.",
            content=content,
            tags=("gepa", "auto-generated", *(tool for tool in tools[:3])),
        )
        return SkillGenerationResult(
            skill=skill, source_run_id=run_id, confidence=confidence
        )

    def _extract_tools(self, events: Sequence[TraceEvent]) -> list[str]:
        """
        从轨迹中提取工具列表。

        功能说明：扫描 TraceEvent.payload 里的 tool 字段，得到去重后的工具名。
        参数说明：events 是执行轨迹。
        返回值：按出现顺序排列的工具名列表。
        设计思路：保留首次出现顺序，比直接 set 更适合生成可读文档。
        使用示例：tools = generator._extract_tools(events)
        """
        tools: list[str] = []
        for event in events:
            tool = event.payload.get("tool", "")
            if tool and tool not in tools:
                # 💡 学习提示：这里不用 set，是因为 Skill 文档里工具出现顺序能帮助理解执行流程。
                tools.append(tool)
        return tools

    def _extract_steps(self, events: Sequence[TraceEvent]) -> list[str]:
        """
        从轨迹中提取执行步骤。

        功能说明：把 agent.step 等事件转换成 Skill 里的 Procedure 步骤。
        参数说明：events 是执行轨迹。
        返回值：步骤文本列表。
        设计思路：优先使用 detail，其次使用 thought，最后给默认兜底，保证生成内容不为空。
        使用示例：steps = generator._extract_steps(events)
        """
        steps: list[str] = []
        for event in events:
            if event.name.endswith("step") or event.name == "agent.step":
                # 💡 学习提示：detail/thought 是不同事件可能使用的字段名，兼容两者能降低 TraceEvent 格式变化的影响。
                detail = event.payload.get(
                    "detail", event.payload.get("thought", "execute next step")
                )
                steps.append(detail)
        return steps or [
            "Inspect task context",
            "Execute required tools",
            "Validate final result",
        ]

    def _render_skill_content(self, steps: Sequence[str], tools: Sequence[str]) -> str:
        """
        渲染 Skill 内容。

        功能说明：把工具和步骤拼成统一格式的 Skill.content。
        参数说明：
            steps：执行步骤列表。
            tools：工具名列表。
        返回值：字符串内容，包含 Tools、Procedure、Validation 三段。
        设计思路：固定模板有利于后续检索、验证和人类阅读。
        使用示例：content = generator._render_skill_content(["check logs"], ["read_file"])
        """
        rendered_steps = "\n".join(
            f"{index + 1}. {step}" for index, step in enumerate(steps)
        )
        rendered_tools = ", ".join(tools) if tools else "no external tools"
        return f"Tools: {rendered_tools}\nProcedure:\n{rendered_steps}\nValidation: verify output against the original task goal."

    def _score_confidence(
        self, success: bool, complexity: ComplexityScore, tools: Sequence[str]
    ) -> float:
        """
        计算 Skill 生成置信度。

        功能说明：综合任务是否成功、复杂度和工具数量，估算这个 Skill 是否可靠。
        参数说明：
            success：来源任务是否成功。
            complexity：复杂度评分。
            tools：本次任务使用的工具。
        返回值：0 到 1 的置信度。
        设计思路：成功任务给更高基线，复杂任务和适量工具使用会提高置信度。
        使用示例：confidence = generator._score_confidence(True, complexity, tools)
        """
        base = 0.5 if success else 0.2
        score = (
            base + complexity.score * 0.35 + min(len(tools), 3) * 0.05
        )  # 💡 学习提示：工具奖励最多算 3 个，避免“堆工具”带来虚高置信度。
        return min(score, 1.0)

    def _normalize_name(self, name: str) -> str:
        """
        标准化 Skill 名称。

        功能说明：把用户可读名称转换成适合保存和检索的 snake_case 名称。
        参数说明：name 是原始 Skill 名称。
        返回值：标准化后的名称字符串。
        设计思路：Skill 名称可能来自用户或 LLM，统一格式能减少重复和路径问题。
        使用示例：generator._normalize_name("Inspect Logs") 返回 "inspect_logs"。
        """
        normalized = re.sub(r"[^a-zA-Z0-9_]+", "_", name.strip().lower()).strip("_")
        if not normalized:
            raise ValueError("normalized skill name must be non-empty")
        return normalized


"""
🤔 思考题：

1. 如果多个事件来自不同 run_id，当前 build_skill() 会有什么问题？你会怎么校验？
2. 为什么 _extract_tools 不直接使用 set？
3. 如果要让 Skill 内容更自然，你会在 _render_skill_content 里改模板，还是引入 LLM？为什么？
4. 置信度里为什么失败任务仍然有 0.2 基线？失败经验有没有复用价值？
5. ⚡ 优化建议：未来可以加入重复 Skill 检测，避免相似轨迹生成大量内容接近的 Skill。
"""
