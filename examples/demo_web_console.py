"""
📦 模块名称：Athena Web Console 启动示例
📍 架构位置：示例层，位于用户手动运行和 FastAPI 应用工厂之间。
🎯 核心作用：演示如何用 Python 脚本启动浏览器可访问的 Athena Web Console。
🔗 依赖关系：依赖 uvicorn、create_app、load_settings；被开发者手动运行学习。
💡 设计思路：示例只做最小启动，不写业务逻辑，确保读者能把注意力放在 create_app() 的复用方式上。
📚 学习重点：看配置如何从 config.yaml 进入 uvicorn，再把 FastAPI app 启动成 HTTP 服务。
"""

from __future__ import annotations

import uvicorn

from athena.api.server import create_app
from athena.config import load_settings


def main() -> None:
    """
    使用 config.yaml 启动 Athena Web Console。

    功能说明：加载配置，创建 FastAPI app，然后交给 uvicorn 运行。
    参数说明：无，配置默认从项目根目录 config.yaml 读取。
    返回值：None；uvicorn.run 会阻塞当前进程直到用户停止服务。
    设计思路：示例脚本和 CLI web 命令走同一个 create_app()，避免两套启动逻辑不一致。
    使用示例：python examples/demo_web_console.py
    """
    settings = load_settings()  # 💡 学习提示：示例不写死端口，统一复用项目配置。
    uvicorn.run(create_app(settings), host=settings.web.host, port=settings.web.port)


if __name__ == "__main__":
    main()  # 💡 学习提示：只有直接运行这个文件时才启动服务，被测试或别的模块导入时不会自动启动。


"""
🤔 思考题：

1. 如果要临时换端口，示例脚本应该读取命令行参数还是改 config.yaml？
2. 为什么示例里不直接 import app 变量，而是调用 create_app()？
3. 如果要在启动前注册一个假 service 做离线演示，你会怎么改？
"""
