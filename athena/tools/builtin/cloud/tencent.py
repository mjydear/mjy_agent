"""
📦 模块名称：腾讯云 CloudOps 客户端适配器
📍 架构位置：CloudOps 云厂商适配层，和 Aliyun/AWS 客户端并列。
🎯 核心作用：提供 tencent provider 标识，让 CloudOps 服务层支持多云切换演示。
🔗 依赖关系：继承 AliyunClient 的演示接口；被 AthenaWebService._cloud_client 使用。
💡 设计思路：MVP 先复用 Mock 行为，只替换厂商标识和环境变量前缀，避免过早接入多个真实 SDK。
📚 学习重点：看 provider_name/env_prefix 如何影响统一结果和环境变量读取。
"""

from __future__ import annotations

from athena.tools.builtin.cloud.aliyun import AliyunClient


class TencentCloudClient(AliyunClient):
    """
    腾讯云演示客户端。

    功能说明：复用 AliyunClient 的演示方法，但把 provider 标识改为 tencent。
    参数说明：构造参数继承自 CloudProviderClient。
    返回值：继承方法返回 CloudOperationResult。
    设计思路：使用继承减少重复 Mock 代码，等接入真实 SDK 时再覆盖具体方法。
    使用示例：TencentCloudClient().check_security_groups()
    """

    provider_name = "tencent"
    env_prefix = "ATHENA_TENCENT"
