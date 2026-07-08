"""
📦 模块名称：CloudOps 结构化诊断报告模型
📍 架构位置：CloudOps 诊断层，位于规则诊断器与 API/前端序列化之间的数据契约。
🎯 核心作用：定义 OpsFinding / OpsDiagnosisReport 两个结构化模型，把“症状-证据-根因-建议-风险”
           标准化，让 API 返回结构化 JSON 而非大段自由文本，LLM 只允许基于报告总结、不得编造证据。
🔗 依赖关系：纯数据模型，仅依赖标准库 dataclasses；被 K8sReadOnlyDiagnoser 生成、被 services 层消费。
💡 设计思路：用 frozen dataclass 表达不可变诊断结论；每条 finding 强绑定 evidence，
           “没有证据就没有结论”，从数据结构上约束幻觉；report 顶层带 summary/metrics/actions 便于前端分区展示。
📚 学习重点：
   1. 为什么 evidence 是必填而不是可选——证据驱动是运维 Agent 可信度的底线。
   2. 为什么 report 单独抽 metrics/raw_evidence——前端“结构化展示 + 可展开原始数据”两种诉求分离。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

from athena.types import JSONValue

# 严重级别集合：与 K8sFinding 保持一致，便于前端统一按等级着色。
VALID_SEVERITIES = ("high", "medium", "low", "info")


@dataclass(frozen=True)
class OpsFinding:
    """
    一条结构化运维诊断结论。

    功能说明：描述某个资源的可观察症状、支撑证据、可能根因、建议动作与风险等级。
    参数说明：
        severity：high/medium/low/info，风险与紧急度。
        resource_kind：资源类型（Pod/Deployment/Service/Node 等）。
        resource_name：资源名称。
        namespace：命名空间（Node 等集群级资源可为空串）。
        symptom：可观察症状（通常是状态或异常现象）。
        evidence：支撑本结论的证据行（事件、日志片段、指标片段）。
        probable_causes：推断的可能根因列表（可为空表示证据不足）。
        recommended_actions：建议的下一步动作列表（只读排障优先）。
    返回值：数据容器。
    设计思路：把原 K8sFinding 的单一 root_cause/recommendation 升级为列表，贴近真实排障“多候选”场景；
        evidence 与结论强绑定，从结构上抑制“无证据编造根因”。
    使用示例：OpsFinding("high", "Pod", "checkout-5f8b", "default", "CrashLoopBackOff", [...], [...], [...])

    🎯 面试考点：为什么 probable_causes 允许为空？答案：证据不足时应显式返回“无法确定根因”，
    而不是硬编一个结论；空列表在语义上就是“证据不足，待人工介入”。
    """

    severity: str
    resource_kind: str
    resource_name: str
    namespace: str
    symptom: str
    evidence: list[str] = field(default_factory=list)
    probable_causes: list[str] = field(default_factory=list)
    recommended_actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, JSONValue]:
        """转为 JSON 友好的 dict，供 API/前端序列化。"""
        return asdict(self)


@dataclass(frozen=True)
class OpsDiagnosisReport:
    """
    一次命名空间级诊断的结构化报告。

    功能说明：聚合一组 OpsFinding，并附带整体摘要、指标、建议动作与原始证据，供前端分区展示。
    参数说明：
        summary：一句话总体结论（如“发现 2 个高危问题”或“证据不足”）。
        namespace：本次诊断的命名空间范围。
        findings：结构化诊断结论列表。
        metrics：聚合统计（如各严重级别计数、扫描的 Pod 数）。
        actions：跨 finding 的整体建议动作（去重后的优先动作）。
        raw_evidence：原始证据快照（前端“可展开查看原始数据”用）。
    返回值：数据容器。
    设计思路：summary/metrics 面向“一眼看清”，findings 面向“逐条排查”，raw_evidence 面向“深挖原始数据”，
        三层信息密度满足不同使用者；LLM 只允许基于本报告做自然语言总结。
    使用示例：OpsDiagnosisReport("发现 2 个高危问题", "default", findings, metrics, actions, raw)

    🎯 面试考点：为什么报告里既有 findings 又有 raw_evidence？答案：findings 是“提炼后的结论”，
    raw_evidence 是“未加工的事实”；保留原始数据让人工可复核、可反驳 Agent 的结论，是可信度关键。
    """

    summary: str
    namespace: str
    findings: list[OpsFinding] = field(default_factory=list)
    metrics: dict[str, JSONValue] = field(default_factory=dict)
    actions: list[str] = field(default_factory=list)
    raw_evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, JSONValue]:
        """转为 JSON 友好的 dict（findings 递归转 dict），供 API/前端序列化。"""
        return {
            "summary": self.summary,
            "namespace": self.namespace,
            "findings": [f.to_dict() for f in self.findings],
            "metrics": self.metrics,
            "actions": self.actions,
            "raw_evidence": self.raw_evidence,
        }
