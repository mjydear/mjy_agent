"""
📦 ASGI 应用入口
📍 架构位置：容器/进程管理器（uvicorn、gunicorn、K8s）加载的模块级 ASGI 对象。
🎯 核心作用：暴露 `app`，供 `uvicorn athena.api.asgi:app` 或 gunicorn 启动，无需 --factory。
"""

from __future__ import annotations

from athena.api.server import create_app

app = create_app()
