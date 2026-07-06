"""
📦 异常分级框架（L0-L4）
📍 架构位置：可观测性/治理层，位于 Agent 执行与告警/人工介入之间。
🎯 核心作用：把任意异常映射为标准严重级别（L0-L4）与处置策略（自动重试/降级/拒绝/人工介入/呼叫值班），
             让线上故障有统一的分级响应，而不是所有错误一视同仁。
🔗 依赖：infra.resilience.is_retryable；athena.exceptions.ErrorCode；可选对接 alert webhook。
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

from athena.exceptions import AthenaError, ErrorCode
from athena.infra.resilience import (
    CircuitBreakerError,
    RateLimitExceeded,
    is_retryable,
)

logger = logging.getLogger(__name__)


class Severity(IntEnum):
    """故障严重级别，数值越大越严重。"""

    L0 = 0  # 信息级：瞬时且已自愈，无需动作
    L1 = 1  # 轻微：可自动重试的瞬时故障
    L2 = 2  # 降级：依赖不可用但可兜底（如大模型挂了走本地规则）
    L3 = 3  # 严重：不可自动恢复的业务/执行失败，需人工介入
    L4 = 4  # 致命：安全/数据风险，需立即呼叫值班


class Strategy(StrEnum):
    """分级对应的处置策略。"""

    IGNORE = "ignore"
    AUTO_RETRY = "auto_retry"
    DEGRADE = "degrade"
    REJECT = "reject"  # 客户端错误（参数非法），直接拒绝不重试
    HUMAN_INTERVENTION = "human_intervention"
    PAGE_ONCALL = "page_oncall"


_SEVERITY_STRATEGY: dict[Severity, Strategy] = {
    Severity.L0: Strategy.IGNORE,
    Severity.L1: Strategy.AUTO_RETRY,
    Severity.L2: Strategy.DEGRADE,
    Severity.L3: Strategy.HUMAN_INTERVENTION,
    Severity.L4: Strategy.PAGE_ONCALL,
}


@dataclass
class Incident:
    """一次被分级后的故障记录。"""

    severity: Severity
    strategy: Strategy
    error_code: str
    message: str
    retryable: bool
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, object]:
        return {
            "severity": self.severity.name,
            "strategy": self.strategy.value,
            "error_code": self.error_code,
            "message": self.message,
            "retryable": self.retryable,
            "created_at": self.created_at,
        }


def classify_incident(exc: BaseException) -> Incident:
    """把异常映射为 (严重级别, 处置策略)。"""
    error_code = (
        exc.code.value if isinstance(exc, AthenaError) else exc.__class__.__name__
    )
    message = str(exc)
    retryable = is_retryable(exc)

    # 安全/权限相关：最高级，可能是沙箱越权或攻击尝试
    if isinstance(exc, PermissionError):
        severity = Severity.L4
    # 参数/类型错误：客户端问题，直接拒绝
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        return Incident(Severity.L1, Strategy.REJECT, error_code, message, False)
    # 限流：轻微，客户端稍后重试
    elif isinstance(exc, RateLimitExceeded):
        severity = Severity.L1
    # 熔断打开：依赖不可用，走降级
    elif isinstance(exc, CircuitBreakerError):
        severity = Severity.L2
    # 可重试的瞬时故障
    elif retryable:
        severity = Severity.L1
    # 大模型调用失败：可降级到本地规则
    elif isinstance(exc, AthenaError) and exc.code == ErrorCode.LLM_CALL_FAILED:
        severity = Severity.L2
    # 工具/Agent 执行失败：需人工排查
    elif isinstance(exc, AthenaError) and exc.code in {
        ErrorCode.TOOL_EXECUTION_FAILED,
        ErrorCode.AGENT_EXECUTION_FAILED,
        ErrorCode.VECTOR_STORE_FAILED,
    }:
        severity = Severity.L3
    else:
        severity = Severity.L3

    return Incident(
        severity=severity,
        strategy=_SEVERITY_STRATEGY[severity],
        error_code=error_code,
        message=message,
        retryable=retryable,
    )


class IncidentManager:
    """
    故障分级记录与统计中心。

    记录每次分级结果，按级别计数；对 L3/L4 触发告警回调（可对接 webhook/钉钉/PagerDuty）。
    """

    def __init__(
        self,
        alert_sink: Callable[[Incident], None] | None = None,
        alert_threshold: Severity = Severity.L3,
    ) -> None:
        self._alert_sink = alert_sink
        self._alert_threshold = alert_threshold
        self._counts: dict[str, int] = {s.name: 0 for s in Severity}
        self._recent: list[Incident] = []

    def record(self, exc: BaseException) -> Incident:
        """分级一个异常，记录统计，必要时触发告警，返回分级结果。"""
        incident = classify_incident(exc)
        self._counts[incident.severity.name] += 1
        self._recent.append(incident)
        if len(self._recent) > 200:
            self._recent.pop(0)
        logger.log(
            logging.ERROR if incident.severity >= Severity.L3 else logging.WARNING,
            "incident severity=%s strategy=%s code=%s message=%s",
            incident.severity.name,
            incident.strategy.value,
            incident.error_code,
            incident.message,
        )
        if incident.severity >= self._alert_threshold and self._alert_sink is not None:
            try:
                self._alert_sink(incident)
            except Exception:  # 告警失败不能反过来影响主流程
                logger.exception("alert sink failed")
        return incident

    def stats(self) -> dict[str, int]:
        return dict(self._counts)

    def recent(self, limit: int = 20) -> list[dict[str, object]]:
        return [i.to_dict() for i in self._recent[-limit:]]
