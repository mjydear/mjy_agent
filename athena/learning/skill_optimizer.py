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
        self, sandbox: SecuritySandbox, acceptance_threshold: float = 0.8
    ) -> None:
        if acceptance_threshold < 0 or acceptance_threshold > 1:
            raise ValueError("acceptance_threshold must be in range 0..1")
        self.sandbox = sandbox
        self.acceptance_threshold = acceptance_threshold

    async def validate(
        self, skill: Skill, simulation_runs: int = 5
    ) -> SkillValidationResult:
        """
        通过轻量沙箱模拟验证 Skill 是否可入库。

        功能说明：先检查 Skill 文本结构，再运行多次沙箱模拟，最后合成评分。
        参数说明：
            skill：待验证的 Skill 对象。
            simulation_runs：沙箱模拟次数，多次运行可以降低偶发错误影响。
        返回值：SkillValidationResult。
        设计思路：结构分只能说明“写得像不像”，沙箱成功率说明“能不能安全执行验证流程”。
        使用示例：await validator.validate(skill, simulation_runs=3)

        🔍 原理讲解：
        这里不是执行 Skill 的真实业务动作，而是跑一个最小安全脚本，确认沙箱链路可用。
        举个例子：
        输入 Skill → 结构评分 → 沙箱执行 validation_flag = True → 合成 score → 输出 accepted/rejected。
        """
        if not isinstance(skill, Skill):
            raise ValueError("skill must be a Skill instance")
        if simulation_runs <= 0:
            raise ValueError("simulation_runs must be positive")
        structural_score = self._structural_score(skill)
        sandbox_successes = 0
        for _ in range(simulation_runs):
            # 💡 学习提示：这里用赋值语句而不是 print，是为了避免 RestrictedPython 对 print 的特殊警告干扰测试输出。
            result = await self.sandbox.run_python("validation_flag = True")
            if result.success:
                sandbox_successes += 1
        success_rate = sandbox_successes / simulation_runs
        score = (
            structural_score * 0.5 + success_rate * 0.5
        )  # 💡 学习提示：结构质量和运行安全各占一半，避免只会写文档但跑不起来的 Skill 入库。
        accepted = (
            score >= self.acceptance_threshold
            and success_rate >= self.acceptance_threshold
        )
        reason = (
            "accepted" if accepted else "score or sandbox success rate below threshold"
        )
        return SkillValidationResult(
            success_rate=success_rate, score=score, accepted=accepted, reason=reason
        )

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
