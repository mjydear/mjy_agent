"""
📦 模块名称：证据约束型 CloudOps 报告摘要器
📍 架构位置：CloudOps 诊断层与 API 服务层之间，负责把 OpsDiagnosisReport 转成可展示摘要或 LLM 输入。
🎯 核心作用：确保 LLM 只能基于 OpsDiagnosisReport 中的结构化证据做总结，禁止补充报告外事实。
🔗 依赖关系：依赖 OpsDiagnosisReport 数据模型与 LLMMessage；被 AthenaWebService 的 K8s 场景调用。
💡 设计思路：把“可供 LLM 使用的事实边界”集中到一个类里，服务层不手写 prompt，测试也能直接断言 prompt 内容。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import TYPE_CHECKING

from athena.infra.llm import LLMMessage
from athena.tools.cloud.k8s.report import OpsDiagnosisReport

if TYPE_CHECKING:
    from athena.infra.llm import LLMClient


class EvidenceBoundReportSummarizer:
    """
    证据约束型报告摘要器。

    功能说明：为 OpsDiagnosisReport 生成确定性摘要，或构造严格限定事实边界的 LLM prompt。
    参数说明：llm_client 可选；缺失时使用 deterministic_summary，保证本地/CI 离线可运行。
    返回值：summarize() 返回最终摘要文本。
    设计思路：LLM 只看 report.to_dict() 的 JSON，不接触原始用户输入、隐含上下文或自由文本 scratchpad，
        从输入边界上限制“编造不存在证据”的空间。
    使用示例：await EvidenceBoundReportSummarizer().summarize(report)
    """

    def __init__(self, llm_client: "LLMClient | None" = None) -> None:
        self.llm_client = llm_client

    def build_messages(self, report: OpsDiagnosisReport) -> list[LLMMessage]:
        """
        构造只包含 OpsDiagnosisReport 的 LLM 消息。

        功能说明：系统提示明确禁止使用报告外事实；用户消息只包含 report JSON。
        参数说明：report 是结构化诊断报告。
        返回值：LLMMessage 列表。
        设计思路：把事实边界收窄到 JSON，后续做审计时可以直接保存这份 prompt。
        使用示例：messages = summarizer.build_messages(report)
        """
        payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
        return [
            LLMMessage(
                role="system",
                content=(
                    "你是 CloudOps 诊断报告摘要器。只能基于用户提供的 "
                    "OpsDiagnosisReport JSON 总结，不得补充 JSON 中不存在的事实、指标、资源或证据。"
                    "如果 evidence/raw_evidence 为空，必须明确说证据不足。"
                ),
            ),
            LLMMessage(
                role="user",
                content=(
                    "请基于以下 OpsDiagnosisReport 输出中文摘要，格式为：总体结论、关键证据、建议动作。\n"
                    f"{payload}"
                ),
            ),
        ]

    async def summarize(self, report: OpsDiagnosisReport) -> str:
        """
        生成报告摘要。

        功能说明：优先调用注入 LLM；无 LLM 或调用失败时回退 deterministic_summary。
        参数说明：report 是结构化诊断报告。
        返回值：中文摘要文本。
        设计思路：生产可接 LLM 提升表达质量，测试/本地默认走确定性摘要，且两条路径共享同一证据边界。
        使用示例：answer = await summarizer.summarize(report)
        """
        if self.llm_client is None:
            return self.deterministic_summary(report)
        try:
            response = await self.llm_client.complete(self.build_messages(report))
        except Exception:
            return self.deterministic_summary(report)
        return response.content.strip() or self.deterministic_summary(report)

    @staticmethod
    def deterministic_summary(report: OpsDiagnosisReport) -> str:
        """
        离线确定性摘要。

        功能说明：完全基于 report 字段拼接摘要，不引入报告外信息。
        参数说明：report 是结构化诊断报告。
        返回值：中文摘要文本。
        设计思路：作为 LLM 失败兜底，也作为单元测试中的“事实边界”基准。
        使用示例：EvidenceBoundReportSummarizer.deterministic_summary(report)
        """
        if not report.findings:
            return f"{report.summary} 当前没有可展示的故障条目；如需继续排查，请补充更多证据。"
        top_findings = report.findings[:3]
        evidence_count = len(report.raw_evidence)
        finding_lines = [
            f"{item.resource_kind}/{item.resource_name}: {item.symptom} ({item.severity})"
            for item in top_findings
        ]
        action_text = "；".join(report.actions[:3]) if report.actions else "暂无建议动作"
        evidence_text = f"已收集 {evidence_count} 条证据" if evidence_count else "证据不足"
        return (
            f"{report.summary} {evidence_text}。"
            f"关键问题：{'；'.join(finding_lines)}。"
            f"建议动作：{action_text}。"
        )


def messages_contain_only_report(messages: Sequence[LLMMessage], report: OpsDiagnosisReport) -> bool:
    """
    测试辅助：检查 LLM 用户消息是否只携带 report JSON 的序列化结果。

    功能说明：确保 prompt 中的动态事实来自 OpsDiagnosisReport，而不是用户原始 task 或隐藏上下文。
    参数说明：messages 是 build_messages 结果；report 是对应报告。
    返回值：True 表示用户消息包含完整 report JSON。
    设计思路：用一个小函数把“证据边界”变成可测试断言。
    使用示例：assert messages_contain_only_report(messages, report)
    """
    expected = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    return bool(messages) and expected in messages[-1].content
