"""
📦 模块名称：Demo6 - 告警触发自动处置演示
📍 架构位置：示例层，连接 AlertWebhookParser 和 FaultDiagnoseWorkflow。
🎯 核心作用：演示 Prometheus 告警如何触发 CloudOps 故障排查工作流。
🔗 依赖关系：依赖 AlertWebhookParser 与 FaultDiagnoseWorkflow；不被核心业务依赖。
💡 设计思路：用最小 webhook payload 模拟 Alertmanager，让完整链路本地可运行。
📚 学习重点：看“外部告警格式 → 内部告警对象 → 故障工作流结果”的转换过程。
"""

from __future__ import annotations

from athena.agent.workflows import FaultDiagnoseWorkflow
from athena.integration.alert_webhook import AlertWebhookParser
from athena.types import JSONValue


def main() -> None:
    """
    运行告警自动处置演示。

    功能说明：构造一条 Alertmanager 风格告警，解析后交给故障工作流处理。
    参数说明：无。
    返回值：None，结果打印到终端。
    设计思路：Demo 只保留最小输入，让你专注理解告警到排障的主链路。
    使用示例：python examples/demo6_alert_auto_handle.py

    🔍 原理讲解：
    输入是一段 webhook JSON，解析器提取 alertname，然后工作流根据 alertname 收集上下文并输出根因。
    这就是 SRE 场景里的“告警自动排查”最小闭环。
    """
    payload: dict[str, JSONValue] = {
        "alerts": [
            {"labels": {"alertname": "KubePodCrashLooping", "severity": "critical"}}
        ]
    }  # 💡 学习提示：这模拟 Alertmanager 推来的最小有效告警。
    alert = AlertWebhookParser().parse(payload)
    result = FaultDiagnoseWorkflow().run(alert.alert_name)
    print("# Alert Auto Handle Demo")
    print(result.summary)
    for step in result.steps:
        print(f"- {step.name}: {step.detail}")


if __name__ == "__main__":
    main()


"""
🤔 思考题：

1. 如果 webhook 里同时有多条告警，当前 Demo 会处理几条？
2. 为什么要先 parse 成 AlertWebhookPayload，而不是直接把 JSON 丢给 workflow？
3. 如果 severity 是 critical，是否应该自动提高人工确认要求？
4. ⚡ 优化建议：未来可以加一个 FastAPI webhook 路由，让真实 Alertmanager 直接 POST 进来。
"""
