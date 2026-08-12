"""
📦 模块名称：Prometheus 查询工具
📍 架构位置：CloudOps 可观测工具层，位于故障工作流和 Prometheus/PromQL 之间。
🎯 核心作用：提供可替换真实 Prometheus 的指标查询与告警上下文收集能力。
🔗 依赖关系：依赖 dataclass 和 JSONValue；被 FaultDiagnoseWorkflow 调用。
💡 设计思路：使用 Mock fallback，让本地没有 Prometheus 也能演示“告警 → 指标上下文”的流程。
📚 学习重点：看 alert_context() 如何把多个 PromQL 查询组合成故障排查上下文。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from athena.types import JSONValue

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PrometheusClient:
    """
    Prometheus/PromQL 小型门面。

    功能说明：封装 PromQL 查询和告警上下文收集。
    参数说明：base_url 是 Prometheus 地址；mock:// 前缀表示演示数据，http(s):// 走真实查询。
    返回值：query 返回单个指标；alert_context 返回告警相关指标集合。
    设计思路：把指标查询包成客户端，真实 HTTP 查询失败时自动降级 Mock。
    使用示例：PrometheusClient("http://localhost:9090").query("up")
    """

    base_url: str = "mock://prometheus"
    timeout_seconds: float = 5.0

    def query(self, promql: str) -> dict[str, JSONValue]:
        """
        执行一次 PromQL 查询。

        功能说明：真实模式经 HTTP 查询 Prometheus，mock/失败时返回确定性演示值。
        参数说明：promql 是 Prometheus 查询表达式。
        返回值：包含 query/value/source 的字典。
        设计思路：真实与演示统一返回结构，上层无需区分数据来源。
        使用示例：client.query("sum(rate(container_cpu_usage_seconds_total[5m]))")
        """
        if not promql.strip():
            raise ValueError("promql must be non-empty")
        if not self.base_url.startswith(("http://", "https://")):
            return self._mock_query(promql)
        try:
            return self._real_query(promql)
        except Exception as exc:
            logger.warning("prometheus query falling back to mock: %s", exc)
            return self._mock_query(promql)

    def _real_query(self, promql: str) -> dict[str, JSONValue]:
        """通过 Prometheus HTTP API 执行即时查询并解析标量/向量首值。"""
        import requests  # 延迟导入，保持工具层可选依赖

        resp = requests.get(
            f"{self.base_url.rstrip('/')}/api/v1/query",
            params={"query": promql},
            timeout=self.timeout_seconds,
        )
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("status") != "success":
            raise ValueError(f"prometheus error: {payload.get('error', 'unknown')}")
        data = payload.get("data", {})
        result = data.get("result", [])
        value: JSONValue = 0.0
        if data.get("resultType") == "scalar" and result:
            value = float(result[1])
        elif result:
            # vector：取第一条时间序列的 value
            value = float(result[0]["value"][1])
        return {"query": promql, "value": value, "source": self.base_url}

    def _mock_query(self, promql: str) -> dict[str, JSONValue]:
        if "container_cpu" in promql:
            value: JSONValue = (
                0.91  # 💡 学习提示：这里故意返回高 CPU，便于故障排查 Demo 展示异常上下文。
            )
        elif "memory" in promql:
            value = 0.84
        else:
            value = 1.0
        return {"query": promql, "value": value, "source": self.base_url}

    def alert_context(self, alert_name: str) -> dict[str, JSONValue]:
        """
        收集告警相关指标上下文。

        功能说明：根据告警名组合 CPU、内存等常用排障指标。
        参数说明：alert_name 是告警名称。
        返回值：包含 alert/cpu/memory 的字典。
        设计思路：工作流只关心上下文，不需要知道每个指标背后的 PromQL 细节。
        使用示例：client.alert_context("KubePodCrashLooping")
        """
        return {
            "alert": alert_name,
            "cpu": self.query("sum(rate(container_cpu_usage_seconds_total[5m]))"),
            "memory": self.query("container_memory_working_set_bytes"),
        }
