"""
📦 模块名称：Skill 优化与验证（Skill Optimizer）
📍 架构位置：GEPA 自进化闭环末段：
              [Skill] → 【SkillValidator】 → [SkillLibrary]
🎯 核心作用：在 Skill 入库前进行质量评分和沙箱验证，避免把失败经验沉淀成可复用技能。
🔗 依赖关系：依赖 memory.skill.Skill 和 tools.sandbox.SecuritySandbox；被 GEPA 自动学习流程调用。
💡 设计思路：采用“准入门禁”模式，结构检查像审核表，沙箱运行像安全演练，两者都通过才允许入库。
📚 学习重点：关注为什么不能把 LLM 生成的 Skill 直接入库，以及如何用可解释规则做第一版质量门槛。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from athena.memory.skill import Skill
from athena.tools.sandbox import SecuritySandbox


@dataclass(frozen=True)
class SkillValidationResult:
    """
    Skill 验证结果。

    功能说明：记录 Skill 验证后的质量指标和是否被接受。
    参数说明：
        success_rate：沙箱模拟执行的成功比例。
        score：结构分和沙箱成功率综合后的总分。
        accepted：是否达到入库标准。
        reason：接受或拒绝的原因。
    返回值：数据容器，不主动执行逻辑。
    设计思路：把 reason 保留下来，方便 Web 页面、日志或面试演示解释“为什么拒绝”。
    使用示例：if validation.accepted: save_skill(skill)
    """

    success_rate: float
    score: float
    accepted: bool
    reason: str


class SkillValidator:
    """
    Skill 质量验证器。

    功能说明：对自动生成的 Skill 做结构评分和沙箱验证。
    参数说明：
        sandbox：安全沙箱，负责执行受限的验证代码。
        acceptance_threshold：接受阈值，默认 0.8。
    返回值：构造函数无返回；validate() 返回 SkillValidationResult。
    设计思路：让 Skill 入库前必须通过“内容完整性”和“执行环境安全性”两道门。
    使用示例：validation = await SkillValidator(SecuritySandbox()).validate(skill)

    🎯 面试考点：为什么要 sandbox？答案：自动生成内容不能默认可信，沙箱能把验证动作限制在安全边界内。
    """

    def __init__(
        self,
        sandbox: SecuritySandbox,
        acceptance_threshold: float = 0.8,
        tool_registry: object | None = None,
    ) -> None:
        if acceptance_threshold < 0 or acceptance_threshold > 1:
            raise ValueError("acceptance_threshold must be in range 0..1")
        self.sandbox = sandbox
        self.acceptance_threshold = acceptance_threshold
        # 注入 ToolRegistry 后，校验 Skill 引用的工具是否真实存在（避免沉淀引用了不存在工具的坏 Skill）。
        self.tool_registry = tool_registry

    async def validate(
        self, skill: Skill, simulation_runs: int = 5
    ) -> SkillValidationResult:
        """
        校验 Skill 是否可入库（结构完整性 + 工具真实可用性）。

        功能说明：结构分检查 Skill 文本是否规范；工具分校验 Skill 引用的工具是否都在
            ToolRegistry 中真实存在（未注入 registry 时该项默认满分，退化为纯结构校验）。
        参数说明：
            skill：待验证的 Skill 对象。
            simulation_runs：保留参数（向后兼容），当前不再跑沙箱空脚本。
        返回值：SkillValidationResult。
        设计思路：替换原“跑 validation_flag=True 空脚本”的假校验——那只证明沙箱活着，
            不证明 Skill 有用。真正有意义的准入是“结构规范 + 引用的工具真实存在”。
        使用示例：await SkillValidator(sandbox, tool_registry=registry).validate(skill)
        """
        if not isinstance(skill, Skill):
            raise ValueError("skill must be a Skill instance")
        if simulation_runs <= 0:
            raise ValueError("simulation_runs must be positive")
        structural_score = self._structural_score(skill)
        tool_score, missing = self._tool_availability_score(skill)
        # 结构与工具可用性各占一半：只会写文档但引用了不存在工具的 Skill 不应入库。
        score = structural_score * 0.5 + tool_score * 0.5
        accepted = (
            score >= self.acceptance_threshold
            and tool_score >= self.acceptance_threshold
        )
        if accepted:
            reason = "accepted"
        elif missing:
            reason = f"references unknown tools: {', '.join(missing)}"
        else:
            reason = "structural or tool-availability score below threshold"
        return SkillValidationResult(
            success_rate=tool_score, score=score, accepted=accepted, reason=reason
        )

    def _tool_availability_score(self, skill: Skill) -> tuple[float, list[str]]:
        """
        校验 Skill 引用的工具是否都在 ToolRegistry 中真实存在。

        功能说明：从 skill.content 的 "Tools: a, b, c" 行解析工具名，逐一比对 registry。
        返回值：(可用比例 0..1, 缺失工具列表)。未注入 registry 时返回 (1.0, [])。
        """
        if self.tool_registry is None:
            return 1.0, []
        available = set(getattr(self.tool_registry, "tools", {}).keys())
        referenced = self._referenced_tools(skill.content)
        if not referenced:
            return 1.0, []  # 无外部工具的 Skill（纯分析步骤）默认满分
        missing = [name for name in referenced if name not in available]
        score = 1.0 - len(missing) / len(referenced)
        return score, missing

    @staticmethod
    def _referenced_tools(content: str) -> list[str]:
        """从 Skill 正文的 'Tools: ...' 行解析引用的工具名列表。"""
        match = re.search(r"Tools:\s*(.+)", content)
        if not match:
            return []
        raw = match.group(1).strip()
        if not raw or raw.lower() == "no external tools":
            return []
        return [tool.strip() for tool in raw.split(",") if tool.strip()]

    def _structural_score(self, skill: Skill) -> float:
        """
        计算 Skill 文本结构分。

        功能说明：检查名称、描述、步骤、验证说明、标签是否齐全。
        参数说明：skill 是待评分的 Skill。
        返回值：0..1 的结构完整度分数。
        设计思路：先用简单可解释规则，让初学者能看懂 Skill 为什么得分。
        使用示例：score = validator._structural_score(skill)
        """
        score = 0.0
        if skill.name.strip():
            score += 0.2
        if len(skill.description.strip()) >= 12:
            score += 0.2
        if "Procedure:" in skill.content:
            score += 0.3
        if "Validation:" in skill.content:
            score += 0.2
        if skill.tags:
            score += 0.1
        return min(score, 1.0)


"""
🤔 思考题：

1. 如果 Skill 内容结构完整，但真实任务执行失败，应该如何改进 validate()？
2. 为什么这里把结构评分和沙箱成功率分开，而不是只看总分？
3. 如果要验证工具调用权限，你会把 PermissionManager 接到哪里？
4. ⚡ 优化建议：未来可以用一组真实小任务做回放验证，而不是只跑最小沙箱脚本。
"""
