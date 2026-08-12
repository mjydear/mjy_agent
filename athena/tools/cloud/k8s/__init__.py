"""
📦 模块名称：K8s 只读诊断工具子包
📍 架构位置：CloudOps 工具层，封装 Kubernetes 只读诊断客户端与工具注册入口。
🎯 核心作用：对外暴露 K8sReadOnlyClient 与 register_k8s_readonly_tools。
🔗 依赖关系：client 依赖 kubernetes 官方 SDK（可选）；tools 依赖 ToolRegistry。
💡 设计思路：__init__ 只做再导出，保持导入路径稳定，隐藏内部文件划分。
"""

from athena.tools.cloud.k8s.actions import (
    K8sActionPlan,
    K8sActionResult,
    K8sActionSecurityPolicy,
    K8sWriteActionExecutor,
)
from athena.tools.cloud.k8s.client import K8sReadOnlyClient
from athena.tools.cloud.k8s.diagnose import K8sFinding, K8sReadOnlyDiagnoser
from athena.tools.cloud.k8s.report import OpsDiagnosisReport, OpsFinding
from athena.tools.cloud.k8s.summarizer import EvidenceBoundReportSummarizer
from athena.tools.cloud.k8s.tools import register_k8s_readonly_tools

__all__ = [
    "EvidenceBoundReportSummarizer",
    "K8sActionPlan",
    "K8sActionResult",
    "K8sActionSecurityPolicy",
    "K8sFinding",
    "K8sReadOnlyClient",
    "K8sReadOnlyDiagnoser",
    "K8sWriteActionExecutor",
    "OpsDiagnosisReport",
    "OpsFinding",
    "register_k8s_readonly_tools",
]
