"""
📦 统一响应体 + 请求链路 ID 上下文
📍 架构位置：接口契约层，所有 RESTful 接口的统一出参外壳。
🎯 核心作用：把 {code, message, data, trace_id, timestamp} 作为统一响应结构，前端只需解析一种格式；
             并用 contextvar 在整条请求链路透传 trace_id，供日志、异常、可观测性关联。
🔗 依赖：pydantic；被 middleware / 全局异常处理 / 各业务路由使用。
"""

from __future__ import annotations

import contextvars
import time
import uuid
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")

# 贯穿单次请求的链路 ID：中间件在入口设置，日志/异常/响应体读取
_trace_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "trace_id", default=""
)


def new_trace_id() -> str:
    """生成一个新的链路 ID（无横线的 uuid4）。"""
    return uuid.uuid4().hex


def set_trace_id(trace_id: str) -> None:
    _trace_id_var.set(trace_id)


def get_trace_id() -> str:
    return _trace_id_var.get()


class ApiResponse(BaseModel, Generic[T]):
    """
    统一响应外壳。

    约定：code=0 表示成功，非 0 表示业务错误码；data 承载真正的业务数据。
    trace_id 默认从当前请求上下文读取，方便前端排障时直接上报。
    """

    code: int = 0
    message: str = "ok"
    data: T | None = None
    trace_id: str = Field(default_factory=get_trace_id)
    timestamp: float = Field(default_factory=time.time)

    @classmethod
    def ok(cls, data: T | None = None, message: str = "ok") -> "ApiResponse[T]":
        return cls(code=0, message=message, data=data)

    @classmethod
    def fail(
        cls, code: int, message: str, data: T | None = None
    ) -> "ApiResponse[T]":
        return cls(code=code, message=message, data=data)
