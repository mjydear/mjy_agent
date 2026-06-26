"""
📦 模块名称：AWS CloudOps 客户端适配器
📍 架构位置：CloudOps 云厂商适配层，和 Aliyun/Tencent 客户端并列。
🎯 核心作用：提供 AWS provider 标识，让同一套资源巡检 Demo 可以切换到 aws。
🔗 依赖关系：继承 AliyunClient 的演示接口；被 AthenaWebService._cloud_client 按 provider 创建。
💡 设计思路：MVP 阶段复用同一组 Mock 数据，只覆盖 provider_name/env_prefix，先验证多云抽象边界。
📚 学习重点：理解“统一接口”比“一开始全量接云 SDK”更适合 MVP。
"""

from __future__ import annotations

from athena.tools.builtin.cloud.aliyun import AliyunClient


class AWSClient(AliyunClient):
    """
    AWS 演示客户端。

    功能说明：复用 AliyunClient 的 list/check/metrics/restart 方法，但在结果中标记 provider=aws。
    参数说明：构造参数继承自 CloudProviderClient。
    返回值：继承方法返回 CloudOperationResult。
    设计思路：先证明服务层可以多云切换，真实 AWS boto3 对接可后续替换。
    使用示例：AWSClient().list_instances()
    """

    provider_name = "aws"
    env_prefix = "ATHENA_AWS"
