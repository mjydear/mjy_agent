"""
📦 模块名称：Prometheus Alertmanager Webhook 解析器
📍 架构位置：外部集成层，位于告警系统和 CloudOps 故障工作流之间。
🎯 核心作用：把 Alertmanager 风格 JSON 转成 Athena 内部统一告警对象。
🔗 依赖关系：依赖 dataclass 和 JSONValue；被告警自动处置 Demo 与未来 Webhook 路由依赖。
💡 设计思路：使用“适配器”模式，把外部系统格式隔离在边界层，内部工作流只处理统一模型。
📚 学习重点：看 parse() 如何做容错，避免外部 payload 缺字段时整个 Agent 崩溃。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from athena.types import JSONValue


@dataclass(frozen=True)
class AlertWebhookPayload:
    """
    标准化后的告警载荷。

    功能说明：保存 Athena 内部真正关心的告警字段。
    参数说明：alert_name 是告警名；severity 是严重级别；labels/annotations 保留原始上下文。
    返回值：数据容器，不主动执行逻辑。
    设计思路：把外部 payload 压成小而稳定的对象，后续工作流不用理解 Alertmanager 的全部字段。
    使用示例：AlertWebhookPayload(alert_name="KubePodCrashLooping", severity="critical")
    """

    alert_name: str
    severity: str = "warning"
    labels: dict[str, JSONValue] = field(default_factory=dict)
    annotations: dict[str, JSONValue] = field(default_factory=dict)


class AlertWebhookParser:
    """
    Alertmanager payload 解析器。

    功能说明：从原始 JSON 中取第一条 alert，并提取 alertname/severity。
    参数说明：parse(payload) 中 payload 是 Alertmanager 或兼容系统传来的字典。
    返回值：AlertWebhookPayload。
    设计思路：外部告警格式可能不稳定，所以解析器在边界处做兜底和类型检查。
    使用示例：AlertWebhookParser().parse({"alerts": [{"labels": {"alertname": "A"}}]})
    """

    def parse(self, payload: dict[str, JSONValue]) -> AlertWebhookPayload:
        """
        解析第一条告警。

        功能说明：兼容 Alertmanager 的 alerts[0].labels 结构，也兼容简化的 alert_name 字段。
        参数说明：payload 是原始 webhook JSON 字典。
        返回值：标准化 AlertWebhookPayload。
        设计思路：只取第一条 alert 是 MVP 选择，先跑通单告警闭环；批量告警可在未来扩展。
        使用示例：parser.parse({"alert_name": "KubePodCrashLooping"})

        🔍 原理讲解：
        Alertmanager 通常把告警放在 alerts 列表里，但演示或其他系统可能直接传 alert_name。
        所以这里先尝试取 alerts[0]，失败时再使用 payload 顶层默认值。
        """
        alerts = payload.get("alerts", [])
        first = (
            alerts[0]
            if isinstance(alerts, list) and alerts and isinstance(alerts[0], dict)
            else {}
        )  # 💡 学习提示：外部系统输入不可信，先判断类型再取下标，避免 IndexError/TypeError。
        labels = first.get("labels", {}) if isinstance(first, dict) else {}
        annotations = first.get("annotations", {}) if isinstance(first, dict) else {}
        if not isinstance(labels, dict):
            labels = (
                {}
            )  # 💡 学习提示：即使 labels 被错误传成字符串，也要降级为空字典，让主流程继续。
        if not isinstance(annotations, dict):
            annotations = {}
        alert_name = str(
            labels.get("alertname", payload.get("alert_name", "KubePodCrashLooping"))
        )
        severity = str(labels.get("severity", "warning"))
        return AlertWebhookPayload(
            alert_name=alert_name,
            severity=severity,
            labels=labels,
            annotations=annotations,
        )


"""
🤔 思考题：

1. 如果一次 webhook 里有 10 条告警，当前只处理第一条会有什么问题？
2. severity 是否应该限制为 warning/critical 等固定枚举？为什么？
3. 如果 labels 里有敏感信息，应该在解析器里脱敏还是在日志层脱敏？
4. ⚡ 优化建议：未来可以返回 list[AlertWebhookPayload]，让工作流逐条处理批量告警。
"""
