"""
📦 模块名称：阿里云 CloudOps 客户端
📍 架构位置：CloudOps 云厂商适配层，位于 CloudProviderClient 基类和资源巡检场景之间。
🎯 核心作用：提供实例查询、安全组检查、监控指标拉取和高危重启操作的演示实现。
🔗 依赖关系：依赖 cloud.base 的操作模型；被 AthenaWebService 的 resource/cost 模式调用。
💡 设计思路：使用 Mock-friendly 适配器，接口形状贴近真实云 SDK，但本地无需真实凭证也能跑通。
📚 学习重点：看每个方法如何先构造 CloudOperation，再交给基类 execute() 做统一安全处理。
"""

from __future__ import annotations

from athena.tools.builtin.cloud.base import (
    CloudOperation,
    CloudProviderClient,
    CloudRiskLevel,
)


class AliyunClient(CloudProviderClient):
    """
    阿里云资源巡检客户端。

    功能说明：封装 CloudOps 需要的几个典型 ECS/安全组/监控操作。
    参数说明：继承 CloudProviderClient，构造参数由父类处理。
    返回值：各方法返回 CloudOperationResult。
    设计思路：子类只声明 provider/env_prefix 和具体操作，公共安全逻辑复用父类。
    使用示例：AliyunClient().list_instances()
    """

    provider_name = "aliyun"
    env_prefix = "ATHENA_ALIYUN"

    def list_instances(self, region: str = "cn-hangzhou"):
        """
        查询 ECS 实例列表。

        功能说明：返回一组 Mock ECS 实例，包含生产实例和闲置实例。
        参数说明：region 是云地域，默认 cn-hangzhou。
        返回值：CloudOperationResult，data.instances 中是实例列表。
        设计思路：用固定数据模拟真实云 API，方便成本优化和资源巡检稳定演示。
        使用示例：client.list_instances("cn-hangzhou")
        """
        operation = CloudOperation(
            name="list_instances",
            risk_level=CloudRiskLevel.READ,
            parameters={"region": region},
        )
        return self.execute(
            operation,
            lambda: {
                "region": region,
                "instances": [
                    {
                        "id": "i-prod-api-01",
                        "name": "prod-api-01",
                        "status": "Running",
                        "cpu": 18.4,
                        "memory": 42.0,
                    },
                    {
                        "id": "i-idle-batch-02",
                        "name": "idle-batch-02",
                        "status": "Running",
                        "cpu": 1.2,
                        "memory": 7.5,
                    },
                ],
            },
        )

    def check_security_groups(self, region: str = "cn-hangzhou"):
        """
        检查安全组风险规则。

        功能说明：返回公开 Web 端口和高风险 SSH 暴露规则。
        参数说明：region 是云地域。
        返回值：CloudOperationResult，data.findings 中是风险发现。
        设计思路：安全组是云运维常见风险点，Mock 一条高风险规则便于演示 Agent 风险识别。
        使用示例：client.check_security_groups()
        """
        operation = CloudOperation(
            name="check_security_groups",
            risk_level=CloudRiskLevel.READ,
            parameters={"region": region},
        )
        return self.execute(
            operation,
            lambda: {
                "region": region,
                "findings": [
                    {
                        "group": "sg-public-web",
                        "risk": "medium",
                        "rule": "0.0.0.0/0:80",
                        "suggestion": "keep if public web is expected",
                    },
                    {
                        "group": "sg-admin",
                        "risk": "high",
                        "rule": "0.0.0.0/0:22",
                        "suggestion": "restrict SSH to bastion CIDR",
                    },
                ],
            },
        )

    def fetch_monitoring_metrics(self, instance_id: str = "i-prod-api-01"):
        """
        拉取实例监控指标。

        功能说明：返回 CPU、内存、网络流入等核心指标。
        参数说明：instance_id 是实例 id。
        返回值：CloudOperationResult，data 中包含指标值。
        设计思路：监控指标是根因分析和成本优化的共同输入，所以单独封装。
        使用示例：client.fetch_monitoring_metrics("i-prod-api-01")
        """
        operation = CloudOperation(
            name="fetch_monitoring_metrics",
            risk_level=CloudRiskLevel.READ,
            parameters={"instance_id": instance_id},
        )
        return self.execute(
            operation,
            lambda: {
                "instance_id": instance_id,
                "cpu_avg": 21.5,
                "memory_avg": 48.2,
                "network_in_mbps": 12.8,
            },
        )

    def restart_instance(self, instance_id: str, confirmed: bool = False):
        """
        重启云主机实例。

        功能说明：构造一个高危重启操作，必须 confirmed=True 才会真正进入 handler。
        参数说明：instance_id 是实例 id；confirmed 表示用户是否已二次确认。
        返回值：CloudOperationResult，未确认时 requires_confirmation=True。
        设计思路：重启会影响线上服务，所以在工具层硬性标记 HIGH，避免只靠前端提醒。
        使用示例：client.restart_instance("i-prod-api-01", confirmed=True)

        🎯 面试考点：为什么 confirmed 要从 API 一直传到工具层？答案：安全边界应靠近真实执行点，不能只相信前端状态。
        """
        operation = CloudOperation(
            name="restart_instance",
            risk_level=CloudRiskLevel.HIGH,
            parameters={"instance_id": instance_id},
            confirmed=confirmed,
        )
        return self.execute(
            operation, lambda: {"instance_id": instance_id, "action": "restart_queued"}
        )
