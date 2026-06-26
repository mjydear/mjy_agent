"""
📦 模块名称：CI/CD 失败诊断原型（CI/CD Diagnostics）
📍 架构位置：外部集成层，位于流水线日志和 Athena 诊断能力之间。
🎯 核心作用：根据流水线失败阶段和日志片段给出初步根因与修复建议。
🔗 依赖关系：只依赖 dataclass；未来可接入多 Agent 工作流、工具执行和知识库检索。
💡 设计思路：先用规则实现可解释原型，再逐步接 LLM 和真实 CI 平台 API。
📚 学习重点：理解生产系统里 Agent 不只是聊天，还要接入工程工作流。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CICDDiagnosticRequest:
    """
    CI/CD 诊断请求。

    功能说明：描述一次流水线失败诊断所需的最小输入。
    参数说明：pipeline_id 是流水线 id；failed_stage 是失败阶段；log_excerpt 是日志片段。
    返回值：数据容器。
    设计思路：先传日志片段而不是完整日志，降低 MVP 输入复杂度。
    使用示例：CICDDiagnosticRequest("p1", "test", "timeout")
    """

    pipeline_id: str
    failed_stage: str
    log_excerpt: str


@dataclass(frozen=True)
class CICDDiagnosticResult:
    """
    CI/CD 诊断结果。

    功能说明：保存根因、建议和置信度。
    参数说明：root_cause 是根因；suggestion 是修复建议；confidence 是可信度。
    返回值：数据容器。
    设计思路：置信度能告诉用户这是强判断还是弱猜测。
    使用示例：CICDDiagnosticResult("timeout", "increase timeout", 0.8)
    """

    root_cause: str
    suggestion: str
    confidence: float


class CICDDiagnostics:
    """
    CI/CD 失败诊断原型。

    功能说明：根据日志关键词给出轻量诊断。
    参数说明：无构造参数。
    返回值：diagnose() 返回 CICDDiagnosticResult。
    设计思路：规则诊断可解释、稳定，适合演示系统边界。
    使用示例：CICDDiagnostics().diagnose(request)
    """

    def diagnose(self, request: CICDDiagnosticRequest) -> CICDDiagnosticResult:
        """
        基于日志片段给出轻量诊断。

        功能说明：识别 timeout、permission 等常见 CI/CD 失败模式。
        参数说明：request 是诊断请求。
        返回值：CICDDiagnosticResult。
        设计思路：先覆盖最常见错误，让接口可跑通；未知错误返回低置信度建议。
        使用示例：diagnostics.diagnose(CICDDiagnosticRequest("1", "build", "timeout"))

        🔍 原理讲解：
        输入：失败阶段 + 日志片段。
        处理过程：小写化日志 → 匹配关键词 timeout/permission → 返回对应建议。
        输出：根因、建议、置信度。
        """
        if not request.pipeline_id.strip() or not request.failed_stage.strip():
            raise ValueError("pipeline_id and failed_stage must be non-empty")
        lowered = (
            request.log_excerpt.lower()
        )  # 💡 学习提示：统一转小写，避免 Timeout/TIMEOUT 这种大小写差异漏匹配。
        if "timeout" in lowered:
            return CICDDiagnosticResult(
                root_cause="timeout",
                suggestion="increase timeout or inspect external dependency latency",
                confidence=0.8,
            )
        if "permission" in lowered:
            return CICDDiagnosticResult(
                root_cause="permission",
                suggestion="check CI token and workspace permissions",
                confidence=0.75,
            )
        return CICDDiagnosticResult(
            root_cause="unknown",
            suggestion="collect full logs and rerun failed stage",
            confidence=0.4,
        )


"""
🤔 思考题：

1. 只靠关键词匹配会有哪些误判？
2. 如果要接入真实 Jenkins/GitHub Actions，需要补哪些字段？
3. 低置信度结果应该自动执行修复吗？为什么？
4. ⚡ 优化建议：未来可以把诊断规则做成可配置表，而不是写死在 if 里。
"""
