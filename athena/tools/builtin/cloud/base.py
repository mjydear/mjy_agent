"""
📦 模块名称：云厂商工具基类
📍 架构位置：CloudOps 工具层底座，位于 Web/API 场景服务和具体云厂商 SDK 之间。
🎯 核心作用：统一云操作风险分级、鉴权读取、重试容错和结果封装。
🔗 依赖关系：依赖 os/time/dataclass；被 aliyun/tencent/aws 工具集与 CloudOps API 服务调用。
💡 设计思路：使用“模板方法 + 结果对象”模式，子类只实现具体云 API，基类负责安全和容错骨架。
📚 学习重点：重点看 risk_level/confirmed 如何控制高危操作，以及 retry() 如何做降级容错。
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from athena.types import JSONValue


class CloudRiskLevel(StrEnum):
    """
    云操作风险等级。

    功能说明：把云操作按 read/write/high 三类风险分级。
    参数说明：枚举值 READ 表示只读，WRITE 表示会改配置，HIGH 表示可能影响生产稳定性。
    返回值：枚举类本身不返回值，被 CloudOperation 使用。
    设计思路：用枚举而不是散落字符串，可以减少拼写错误，也便于统一判断高危操作。
    使用示例：CloudRiskLevel.HIGH

    🎯 面试考点：为什么要做风险分级？答案：生产运维不能让 Agent 不加控制地执行重启、删除、放通安全组等动作。
    """

    READ = "read"
    WRITE = "write"
    HIGH = "high"


@dataclass(frozen=True)
class CloudOperation:
    """
    一次云操作请求。

    功能说明：描述即将执行的云操作名称、风险级别、参数和是否已确认。
    参数说明：name 是操作名；risk_level 是风险等级；parameters 是操作参数；confirmed 是人工确认标记。
    返回值：数据容器，不主动执行操作。
    设计思路：把“要做什么”和“怎么执行”分开，execute() 可以统一做安全检查。
    使用示例：CloudOperation("restart_instance", CloudRiskLevel.HIGH, confirmed=True)
    """

    name: str
    risk_level: CloudRiskLevel
    parameters: dict[str, JSONValue] = field(
        default_factory=dict
    )  # 💡 学习提示：参数单独保存，便于审计日志记录“Agent 想操作什么”。
    confirmed: bool = False


@dataclass(frozen=True)
class CloudOperationResult:
    """
    云操作统一返回结果。

    功能说明：封装云 API 调用是否成功、返回数据、风险状态、耗时和审计 id。
    参数说明：success 表示执行成功；requires_confirmation 表示被高危确认拦截；audit_id 用于追踪。
    返回值：数据容器，被服务层转换成 Web API 响应。
    设计思路：无论具体云厂商返回什么格式，上层都只处理这一种结果对象。
    使用示例：result.success 判断云操作是否完成。
    """

    success: bool
    provider: str
    operation: str
    risk_level: CloudRiskLevel
    data: dict[str, JSONValue]
    message: str
    duration_ms: float
    requires_confirmation: bool = False
    audit_id: str | None = None


class CloudProviderClient:
    """
    云厂商客户端基类。

    功能说明：提供鉴权读取、高危确认、重试容错和统一结果封装。
    参数说明：account 是账号标识；max_retries 是失败后的最多尝试次数。
    返回值：execute() 返回 CloudOperationResult。
    设计思路：这是“模板方法”的骨架：子类提供具体 handler，基类负责安全和通用流程。
    使用示例：client.execute(operation, lambda: {"ok": True})
    """

    provider_name = "base"
    env_prefix = "ATHENA_CLOUD"

    def __init__(self, account: str = "default", max_retries: int = 3) -> None:
        """
        初始化云厂商客户端。

        功能说明：保存账号、重试次数，并从环境变量读取云厂商密钥。
        参数说明：account 用来区分多账号；max_retries 控制失败重试次数。
        返回值：None。
        设计思路：密钥读取集中在基类，子类不用重复写鉴权入口；Mock fallback 让本地演示无需真实云账号。
        使用示例：CloudProviderClient(account="prod", max_retries=2)
        """
        if max_retries <= 0:
            raise ValueError("max_retries must be positive")
        self.account = account
        self.max_retries = max_retries
        self.access_key_id = os.getenv(
            f"{self.env_prefix}_ACCESS_KEY_ID", "mock-access-key"
        )  # 💡 学习提示：生产环境应使用真实环境变量，本地 demo 用 mock 值保证可运行。
        self.access_key_secret = os.getenv(
            f"{self.env_prefix}_ACCESS_KEY_SECRET", "mock-secret"
        )

    def execute(
        self, operation: CloudOperation, handler: Callable[[], dict[str, JSONValue]]
    ) -> CloudOperationResult:
        """
        执行带高危确认和重试保护的云操作。

        功能说明：先拦截未确认的高危操作，再调用 handler 执行真实/Mock 云 API。
        参数说明：operation 描述操作元数据；handler 是真正访问云 API 的函数。
        返回值：CloudOperationResult，统一表达成功、失败或等待确认。
        设计思路：handler 只关心云厂商细节，execute 统一处理权限、重试、耗时和审计。
        使用示例：client.execute(operation, lambda: {"instances": []})

        🔍 原理讲解：
        这里像一个“安全闸门”：所有云操作都必须先经过 execute。
        输入 CloudOperation → 检查是否高危且未确认 → 需要确认则立即返回 → 否则重试调用 handler → 输出统一结果。
        """
        started_at = time.perf_counter()
        if operation.risk_level is CloudRiskLevel.HIGH and not operation.confirmed:
            return CloudOperationResult(
                success=False,
                provider=self.provider_name,
                operation=operation.name,
                risk_level=operation.risk_level,
                data={"parameters": operation.parameters},
                message="high risk operation requires human confirmation",
                duration_ms=(time.perf_counter() - started_at) * 1000,
                requires_confirmation=True,
                audit_id=self._audit_id(operation.name),
            )
        last_error: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                data = (
                    handler()
                )  # 💡 学习提示：真正的云 SDK 调用被包在 handler 里，基类不用知道阿里云/腾讯云具体 API 名。
                return CloudOperationResult(
                    success=True,
                    provider=self.provider_name,
                    operation=operation.name,
                    risk_level=operation.risk_level,
                    data={"account": self.account, "attempt": attempt, **data},
                    message="operation completed",
                    duration_ms=(time.perf_counter() - started_at) * 1000,
                    audit_id=self._audit_id(operation.name),
                )
            except Exception as exc:
                last_error = exc
                time.sleep(
                    min(0.05 * 2 ** (attempt - 1), 0.2)
                )  # 💡 学习提示：指数退避能避免网络抖动时立刻连续打爆外部 API。
        return CloudOperationResult(
            success=False,
            provider=self.provider_name,
            operation=operation.name,
            risk_level=operation.risk_level,
            data={"account": self.account, "error": str(last_error)},
            message="operation failed after retries",
            duration_ms=(time.perf_counter() - started_at) * 1000,
            audit_id=self._audit_id(operation.name),
        )

    def _audit_id(self, operation: str) -> str:
        """
        生成简易审计 id。

        功能说明：把云厂商、操作名和时间戳拼成可追踪 id。
        参数说明：operation 是操作名。
        返回值：审计 id 字符串。
        设计思路：MVP 不接审计数据库，先保证每次操作都有可读追踪标识。
        使用示例：client._audit_id("list_instances")
        """
        return f"{self.provider_name}-{operation}-{int(time.time() * 1000)}"


"""
🤔 思考题：

1. 如果要把 retry 从同步 sleep 改成异步，你会把 execute 改成 async 吗？
2. 高危操作为什么在进入 handler 前就拦截？
3. 当前密钥有 mock fallback，生产环境是否应该强制要求真实环境变量？
4. ⚡ 优化建议：未来可以把 audit_id 写入 AuditLogger，实现持久化审计。
"""
