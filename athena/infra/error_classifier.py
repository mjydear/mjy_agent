"""
📦 模块名称：Agent 任务异常分级器（Error Classifier L0-L4）
📍 架构位置：基础设施层，被 Agent 执行循环和任务编排器调用。
🎯 核心作用：将 Agent 运行过程中的异常按严重程度分级（L0-L4），
   不同级别对应不同处理策略（自动重试、降级、人工介入），
   避免"一刀切"的异常处理导致的小问题阻塞或大问题被忽略。
🔗 依赖关系：独立模块，被 athena.agent.core 和 athena.api.task_manager 使用。
💡 设计思路：基于文本规则的确定性分类器，参考 LIBAI 项目 L0-L6 分类体系，
   针对云运维 Agent 场景定制为 L0-L4 五级。

🎯 面试考点：
   1. 为什么不用 ML 分类而用规则？-> 异常分类需要确定性，ML 分类的不可解释性在运维场景不可接受
   2. L0-L4 的划分依据？-> 基于"是否可自动恢复"和"是否需要人工介入"两个维度
   3. 分类结果如何驱动后续处理？-> 不同级别映射到不同的 RecoveryAction
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

logger = logging.getLogger(__name__)


class ErrorLevel(Enum):
    """异常分级（L0-L4），严重程度递增。"""

    L0 = 0  # 瞬时故障：网络抖动、超时，自动重试可恢复
    L1 = 1  # 降级可恢复：外部依赖临时不可用，可降级到备用方案
    L2 = 2  # 可恢复故障：重试+退避后通常可恢复，但需关注
    L3 = 3  # 部分失败：部分操作失败，需降级处理，建议通知运维
    L4 = 4  # 严重故障：需要人工介入，自动处理不可靠


class RecoveryAction(Enum):
    """各级别对应的恢复策略。"""

    AUTO_RETRY = auto()  # 自动重试（L0）
    FALLBACK = auto()  # 降级备用（L1）
    RETRY_WITH_BACKOFF = auto()  # 退避重试（L2）
    DEGRADE_AND_NOTIFY = auto()  # 降级 + 通知（L3）
    HUMAN_INTERVENTION = auto()  # 人工介入（L4）


# 级别 → 恢复策略映射
_LEVEL_TO_ACTION: dict[ErrorLevel, RecoveryAction] = {
    ErrorLevel.L0: RecoveryAction.AUTO_RETRY,
    ErrorLevel.L1: RecoveryAction.FALLBACK,
    ErrorLevel.L2: RecoveryAction.RETRY_WITH_BACKOFF,
    ErrorLevel.L3: RecoveryAction.DEGRADE_AND_NOTIFY,
    ErrorLevel.L4: RecoveryAction.HUMAN_INTERVENTION,
}


@dataclass
class ClassifiedError:
    """分类后的异常信息。"""

    level: ErrorLevel
    action: RecoveryAction
    original_error: Exception
    error_type: str  # 异常类型名称
    error_message: str  # 清洗后的错误消息
    source: str  # 异常来源（llm/tool/vector_db/k8s/api）
    suggestion: str  # 给运维人员的处理建议

    @property
    def is_auto_recoverable(self) -> bool:
        """是否可以自动恢复（L0-L2）。"""
        return self.level.value <= 2

    @property
    def needs_human(self) -> bool:
        """是否需要人工介入（L3-L4）。"""
        return self.level.value >= 3


# ---- 分类规则 ----

# 每个级别由一组 (来源, 关键词列表) 规则定义
# 匹配优先级：L4 > L3 > L2 > L1 > L0（严重级别优先匹配）


# L4 - 严重故障：需要人工介入
_L4_RULES: dict[str, list[str]] = {
    "llm": [
        "invalid api key", "invalid_api_key", "authentication failed",
        "billing", "quota exceeded", "account suspended",
    ],
    "tool": [
        "permission denied", "access denied", "forbidden",
        "kernel panic", "disk full", "out of memory", "oom",
    ],
    "vector_db": [
        "collection dropped", "index corrupted", "disk full",
        "authentication failed", "unauthorized",
    ],
    "k8s": [
        "forbidden", "unauthorized", "cluster not found",
    ],
    "api": [
        "internal server error", "database connection failed",
        "redis connection failed",
    ],
}

# L3 - 部分失败：降级处理，通知运维
_L3_RULES: dict[str, list[str]] = {
    "llm": [
        "context length", "context_length", "token limit",
        "content filter", "content_filter", "safety",
    ],
    "tool": [
        "command not found", "execution failed", "non-zero exit",
        "operation timeout", "resource not found",
    ],
    "vector_db": [
        "search timeout", "insert failed", "index build failed",
    ],
    "k8s": [
        "not found", "resource not found", "namespace not found",
        "pod not found", "deployment not found",
    ],
    "api": [
        "validation error", "bad request", "not found",
        "task not found",
    ],
}

# L2 - 可恢复故障：退避重试后通常可恢复
_L2_RULES: dict[str, list[str]] = {
    "llm": [
        "server error", "service unavailable", "overloaded",
        "capacity", "internal error",
    ],
    "tool": [
        "connection refused", "connection reset",
    ],
    "vector_db": [
        "connection refused", "server error",
    ],
    "k8s": [
        "server error", "service unavailable",
    ],
    "api": [
        "service unavailable",
        "task timeout",
    ],
}

# L1 - 降级可恢复：外部依赖临时不可用
_L1_RULES: dict[str, list[str]] = {
    "llm": [
        "rate limit", "rate_limit", "too many requests",
    ],
    "tool": [],
    "vector_db": [
        "rate limit", "too many requests",
    ],
    "k8s": [
        "rate limit", "too many requests",
    ],
    "api": [
        "rate limit", "too many requests",
    ],
}

# L0 - 瞬时故障：网络抖动、超时
_L0_RULES: dict[str, list[str]] = {
    "llm": [
        "timeout", "timed out", "connection error",
        "network", "dns", "name resolution",
    ],
    "tool": [
        "timeout", "timed out",
    ],
    "vector_db": [
        "timeout", "timed out", "connection error",
    ],
    "k8s": [
        "timeout", "timed out", "connection error",
    ],
    "api": [
        "timeout", "timed out", "connection error",
    ],
}

# 按优先级排列的规则列表
_RULE_TABLE: list[tuple[ErrorLevel, dict[str, list[str]]]] = [
    (ErrorLevel.L4, _L4_RULES),
    (ErrorLevel.L3, _L3_RULES),
    (ErrorLevel.L2, _L2_RULES),
    (ErrorLevel.L1, _L1_RULES),
    (ErrorLevel.L0, _L0_RULES),
]


def _detect_source(error: Exception) -> str:
    """根据异常类型和错误消息推断异常来源。

    规则优先级：
    1. 如果异常有 source 属性（如 LLMError），直接使用
    2. 根据异常类型名称推断
    3. 根据错误消息中的关键词推断
    """
    # 检查 source 属性
    if hasattr(error, "source"):
        return getattr(error, "source", "unknown")

    # 根据异常类型推断
    error_type = type(error).__name__.lower()
    if "llm" in error_type:
        return "llm"
    if "tool" in error_type or "sandbox" in error_type:
        return "tool"
    if "vector" in error_type or "milvus" in error_type or "embedding" in error_type:
        return "vector_db"
    if "k8s" in error_type or "kubernetes" in error_type:
        return "k8s"
    if "api" in error_type or "http" in error_type:
        return "api"

    # 根据错误消息关键词推断
    msg = str(error).lower()
    keyword_to_source = [
        (["llm", "model", "token", "openai", "claude", "deepseek"], "llm"),
        (["tool", "sandbox", "command", "execution"], "tool"),
        (["vector", "milvus", "embedding", "collection"], "vector_db"),
        (["k8s", "kubernetes", "pod", "deployment", "cluster"], "k8s"),
    ]
    for keywords, source in keyword_to_source:
        for kw in keywords:
            if kw in msg:
                return source

    return "unknown"


def _generate_suggestion(level: ErrorLevel, source: str, error_message: str) -> str:
    """根据级别和来源生成运维建议。"""
    suggestions = {
        (ErrorLevel.L0, "llm"): "网络抖动导致 LLM 调用超时，自动重试中。检查网络连接和 DNS 解析。",
        (ErrorLevel.L0, "tool"): "工具执行超时，自动重试中。检查目标主机连通性和防火墙规则。",
        (ErrorLevel.L0, "vector_db"): "向量库连接超时，自动重试中。检查 Milvus/向量库服务状态。",
        (ErrorLevel.L1, "llm"): "LLM API 限流，切换备用模型或等待配额恢复。建议检查 API 配额使用情况。",
        (ErrorLevel.L1, "vector_db"): "向量库限流，降低检索并发或切换到本地缓存。",
        (ErrorLevel.L2, "llm"): "LLM 服务端异常，退避重试中。若持续失败，检查 API 提供商状态页。",
        (ErrorLevel.L2, "tool"): "工具执行失败，退避重试中。检查目标服务是否正常运行。",
        (ErrorLevel.L2, "vector_db"): "向量库服务异常，退避重试中。检查向量库进程和资源使用。",
        (ErrorLevel.L3, "llm"): "LLM 返回内容过滤或上下文超限。建议截断输入或切换模型。",
        (ErrorLevel.L3, "tool"): "工具执行失败，任务已降级。检查命令语法和目标资源是否存在。",
        (ErrorLevel.L3, "vector_db"): "向量检索失败，已降级到关键词匹配。检查索引状态。",
        (ErrorLevel.L4, "llm"): "LLM API 鉴权失败或配额耗尽。请检查 API Key 和账单状态。",
        (ErrorLevel.L4, "tool"): "工具执行权限不足或系统资源耗尽。需要运维人员介入排查。",
        (ErrorLevel.L4, "vector_db"): "向量库严重故障。请检查磁盘空间、索引完整性和认证配置。",
        (ErrorLevel.L4, "k8s"): "K8s 集群访问权限异常。请检查 kubeconfig 和 RBAC 配置。",
    }

    key = (level, source)
    if key in suggestions:
        return suggestions[key]

    # 通用建议
    default_suggestions = {
        ErrorLevel.L0: "瞬时故障，自动重试中。",
        ErrorLevel.L1: "外部依赖限流，已启用降级方案。",
        ErrorLevel.L2: "服务异常，退避重试中。若持续失败请关注。",
        ErrorLevel.L3: "部分功能降级，建议通知运维人员。",
        ErrorLevel.L4: f"严重故障，需要人工介入：{error_message[:200]}",
    }
    return default_suggestions.get(level, "未知异常级别。")


def classify_error(error: Exception, source: str | None = None) -> ClassifiedError:
    """对异常进行分级分类。

    分类逻辑（按优先级从高到低匹配）：
    1. L4 严重故障 → 鉴权失败、配额耗尽、系统资源耗尽
    2. L3 部分失败 → 上下文超限、内容过滤、工具执行失败
    3. L2 可恢复故障 → 服务端错误、连接拒绝
    4. L1 降级可恢复 → 限流
    5. L0 瞬时故障 → 超时、网络抖动
    6. 默认 L2 → 未知异常保守处理

    Args:
        error:  原始异常对象
        source: 异常来源（llm/tool/vector_db/k8s/api），None 则自动检测

    Returns:
        ClassifiedError 包含级别、恢复策略、建议等信息

    使用示例：
        try:
            result = await llm.complete(messages)
        except Exception as e:
            classified = classify_error(e)
            if classified.is_auto_recoverable:
                # 自动重试或降级
                ...
            else:
                # 需要人工介入
                ...
    """
    detected_source = source or _detect_source(error)
    error_message = str(error)
    message_lower = error_message.lower()

    # 按优先级从 L4 到 L0 匹配
    for level, rules in _RULE_TABLE:
        source_rules = rules.get(detected_source, [])
        for keyword in source_rules:
            if keyword in message_lower:
                action = _LEVEL_TO_ACTION[level]
                suggestion = _generate_suggestion(level, detected_source, error_message)
                logger.info(
                    "Error classified: L%d [%s/%s] → %s: %s",
                    level.value, detected_source, action.name, suggestion[:100], error_message[:200],
                )
                return ClassifiedError(
                    level=level,
                    action=action,
                    original_error=error,
                    error_type=type(error).__name__,
                    error_message=error_message,
                    source=detected_source,
                    suggestion=suggestion,
                )

    # 默认 L2：未知异常保守处理
    action = _LEVEL_TO_ACTION[ErrorLevel.L2]
    suggestion = f"未识别的异常类型，退避重试中：{error_message[:200]}"
    logger.warning(
        "Unclassified error (default L2): %s [%s]: %s",
        type(error).__name__, detected_source, error_message[:200],
    )
    return ClassifiedError(
        level=ErrorLevel.L2,
        action=action,
        original_error=error,
        error_type=type(error).__name__,
        error_message=error_message,
        source=detected_source,
        suggestion=suggestion,
    )


def get_recovery_action(level: ErrorLevel) -> RecoveryAction:
    """获取指定级别的恢复策略。"""
    return _LEVEL_TO_ACTION[level]


def get_level(level_value: int) -> ErrorLevel:
    """根据整数值获取 ErrorLevel。"""
    for level in ErrorLevel:
        if level.value == level_value:
            return level
    raise ValueError(f"Invalid error level: {level_value}")