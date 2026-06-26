"""
📦 模块名称：FastAPI 路由依赖工具
📍 架构位置：路由公共基础设施，位于各 route 文件和 FastAPI app.state 之间。
🎯 核心作用：提供 get_service()，让每个路由都能拿到同一个 AthenaWebService 实例。
🔗 依赖关系：依赖 FastAPI Request 和 AthenaWebService；被所有 api/routes/*.py 依赖。
💡 设计思路：使用 FastAPI 的依赖注入机制，把共享服务从 app.state 中取出，避免全局变量散落各处。
📚 学习重点：理解 Depends(get_service) 如何把 Web 应用状态注入到路由函数参数里。
"""

from __future__ import annotations

from typing import cast

from fastapi import Request

from athena.api.services import AthenaWebService


def get_service(request: Request) -> AthenaWebService:
    """
    从 FastAPI 应用状态中取出共享服务层对象。

    功能说明：给路由函数提供 AthenaWebService。
    参数说明：request 是 FastAPI 自动传入的当前请求对象。
    返回值：AthenaWebService 实例。
    设计思路：服务对象在 create_app() 中保存到 app.state，这里统一读取，路由就不用知道创建细节。
    使用示例：async def route(service: AthenaWebService = Depends(get_service))

    🎯 面试考点：为什么不用模块级全局 service？答案：app.state 更利于测试注入，也避免多个 app 实例互相污染。
    """
    return cast(
        AthenaWebService, request.app.state.service
    )  # 💡 学习提示：cast 只帮助类型检查器理解类型，运行时不会转换对象。


"""
🤔 思考题：

1. 如果未来有多个 service，例如 AuthService、BillingService，你会怎么组织依赖函数？
2. 如果 create_app() 忘记设置 app.state.service，这里会发生什么？
3. 为什么测试时注入 service 比 monkeypatch 全局变量更清晰？
"""
