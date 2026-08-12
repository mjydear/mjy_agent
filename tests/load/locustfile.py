"""Athena Agent 压测脚本（Locust）。

用法：
    pip install locust
    locust -f tests/load/locustfile.py --host http://127.0.0.1:8000

场景：混合读写——健康探针、指标查询、工作流执行。
可配置环境变量 ATHENA_API_KEY 携带鉴权头（启用鉴权时）。
"""

from __future__ import annotations

import os

from locust import HttpUser, between, task


def _headers() -> dict[str, str]:
    api_key = os.getenv("ATHENA_API_KEY", "")
    return {"X-API-Key": api_key} if api_key else {}


class AthenaUser(HttpUser):
    """模拟一个持续访问 Athena API 的用户。"""

    wait_time = between(0.1, 1.0)

    @task(5)
    def healthz(self) -> None:
        # 高频轻量探针，占比最大
        self.client.get("/healthz", name="GET /healthz")

    @task(3)
    def metrics(self) -> None:
        self.client.get("/api/metrics", name="GET /api/metrics", headers=_headers())

    @task(2)
    def run_workflow(self) -> None:
        # 写路径：触发一次多 Agent 工作流
        self.client.post(
            "/api/workflow/run",
            json={"task": "巡检集群; 汇总告警"},
            name="POST /api/workflow/run",
            headers=_headers(),
        )
