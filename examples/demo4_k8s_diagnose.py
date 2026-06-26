"""
📦 模块名称：Demo4 - K8s Pod 故障诊断演示
📍 架构位置：示例层，位于用户手动运行入口和 CloudOps K8s 工具链之间。
🎯 核心作用：演示如何收集 K8s 快照并输出常见故障诊断结果。
🔗 依赖关系：依赖 K8sOpsTools 和 K8sDiagnoser；不被核心业务依赖，只供学习和演示。
💡 设计思路：使用 Mock K8s 数据，保证没有真实集群时也能跑通诊断流程。
📚 学习重点：看 snapshot 和 diagnoses 分别代表“现状数据”和“诊断结论”。
"""

from __future__ import annotations

from collections.abc import Sized
from typing import cast

from athena.tools.builtin.k8s import K8sDiagnoser, K8sOpsTools


def main() -> None:
    """
    运行 K8s 诊断演示。

    功能说明：创建工具箱和诊断器，打印集群规模和每条诊断建议。
    参数说明：无。
    返回值：None，结果直接输出到终端。
    设计思路：Demo 保持线性流程，方便初学者顺着“采集 → 诊断 → 输出”阅读。
    使用示例：python examples/demo4_k8s_diagnose.py
    """
    tools = (
        K8sOpsTools()
    )  # 💡 学习提示：默认使用 Mock 客户端，所以本机不需要 kubeconfig。
    diagnoser = K8sDiagnoser(
        tools.client
    )  # 💡 学习提示：诊断器复用同一个 client，确保快照和诊断看到的是同一批数据。
    snapshot = tools.cluster_snapshot()
    pods = cast(Sized, snapshot["pods"])
    nodes = cast(Sized, snapshot["nodes"])
    diagnoses = diagnoser.diagnose_pods()
    print("# K8s Diagnosis Demo")
    print(f"Pods: {len(pods)}, Nodes: {len(nodes)}")
    for diagnosis in diagnoses:
        print(f"- {diagnosis.pod}: {diagnosis.symptom} -> {diagnosis.recommendation}")


if __name__ == "__main__":
    main()


"""
🤔 思考题：

1. 如果要接真实 K8s 集群，你会从哪里替换 Mock 客户端？
2. 这个 Demo 为什么先打印 Pod/Node 数量，再打印诊断结果？
3. 如果 diagnoses 为空，终端输出应该怎么更友好？
4. ⚡ 优化建议：未来可以把输出改成表格，展示 severity、root_cause 和 recommendation。
"""
