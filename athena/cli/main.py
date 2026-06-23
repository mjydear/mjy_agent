"""
📦 模块名称：CLI 入口（Command Line Interface Entrypoint）
📍 架构位置：用户接口层（UI Layer）—— 整个架构的最顶层，是用户与 Agent 交互的入口：
           [用户终端输入] → 【本文件 CLI】 → [build_agent()] → [ReActAgent]
🎯 核心作用：把用户在终端输入的命令和问题，转交给 Agent 处理，再把结果打印回来
🔗 依赖关系：
   - 依赖：
     * typer          → CLI 框架（定义命令、参数、输出颜色）
     * athena.config  → 读取配置文件（model、max_steps 等）
     * athena.agent   → ReActAgent（真正的大脑）
     * 其他所有模块   → build_agent() 把它们全部组装起来
   - 被依赖：无（CLI 是顶层，没有其他模块依赖它）
         不过 setup.py 中的 entry_points 指向这里的 main()
💡 设计思路：
   ① "组合根"模式（Composition Root）——所有组件的组装集中在 build_agent() 一处，
      避免依赖关系散落在各个地方，便于修改和测试。
   ② Typer 框架——比 argparse 更简洁，用类型注解自动生成帮助文档和参数校验。
   ③ asyncio.run()——CLI 本身是同步的，Agent 是异步的，用 asyncio.run() 做桥接。
   ④ 只捕获 AthenaError——明确的业务错误显示友好信息，未知异常让它正常崩溃（方便调试）。
📚 学习重点：
   1. build_agent() 的"组合根"模式——所有依赖在一处装配
   2. asyncio.run() 如何让同步代码调用异步函数
   3. Typer 的装饰器风格 CLI 定义（@app.command()）
   4. typer.Exit(code=1) 和退出码的含义
   5. if __name__ == "__main__" 的作用
"""

from __future__ import annotations  # 💡 学习提示：支持类型注解前向引用，全项目统一风格

import asyncio    # 💡 学习提示：CLI 是同步的，Agent 是异步的，asyncio.run() 是连接两者的桥梁
from pathlib import Path

import typer      # 💡 学习提示：Typer 是基于 Click 的 CLI 框架，用 Python 类型注解自动生成帮助文档

from athena.agent import ReActAgent
from athena.config import load_settings
from athena.exceptions import AthenaError
from athena.infra.llm import LLMClientFactory
from athena.logging import configure_logging
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry
from athena.tools.builtin.basic import register_basic_tools

# 💡 学习提示：typer.Typer() 创建 CLI 应用实例，help 参数会显示在 `athena --help` 里
# 这个 app 对象扮演"命令注册表"的角色，后面的 @app.command() 都是注册进它里面的
app = typer.Typer(help="Athena Agent command line interface.")


def build_agent(config_path: Path | None = None) -> ReActAgent:
    """
    从配置文件组装一个完整可用的 ReActAgent（组合根函数）。

    功能说明：
        这是整个项目的"装配工厂"——把散落的各个组件（LLM客户端、记忆、工具、提示词）
        按照配置文件的参数，组装成一个可以立刻使用的 Agent 实例。
        就像组装一台电脑：读取配置单（config），然后把 CPU、内存、硬盘都装进机箱。

    参数说明：
        config_path: 配置文件的路径（可选）。
                     传入 None 时自动查找当前目录的 config.yaml。
                     示例：Path("D:/my-project/custom_config.yaml")

    返回值：
        配置完毕、可以直接调用 .run() 的 ReActAgent 实例

    异常：
        AthenaError / ConfigError: 配置文件格式错误或 API Key 未设置时抛出
        （调用方负责捕获并显示友好错误信息）

    设计思路：
        "组合根"模式（Composition Root Pattern）：
        所有依赖的创建和连接集中在一个地方（这个函数），而不是散落在各处。
        好处：
        ① 看一眼就知道 Agent 需要哪些组件（LLM + 工具 + 记忆 + 提示词）
        ② 修改组件（换模型、换记忆实现）只需改这一个函数
        ③ 测试时可以单独调用这个函数，验证整个组装过程

    # 🎯 面试考点：为什么把 Agent 的组装单独抽成 build_agent() 而不是写在 chat() 里？
    # 答：① 复用——chat() 和 start() 两个命令都需要 Agent，不重复写
    #     ② 可测试性——可以单独测试 build_agent()，不依赖 CLI 命令执行
    #     ③ 单一职责——CLI 命令只负责"用户交互"，不负责"组件装配"

    使用示例：
        # 使用默认配置（读取 config.yaml）
        agent = build_agent()

        # 使用指定配置文件
        agent = build_agent(Path("custom_config.yaml"))
    """
    settings = load_settings(config_path)  # 💡 学习提示：读取并验证配置文件，所有参数都有默认值，找不到文件也能运行
    configure_logging(settings.logging.level)  # 💡 学习提示：在最早的时机初始化日志，确保后续所有组件的日志都能被记录

    registry = ToolRegistry()
    register_basic_tools(registry)  # 💡 学习提示：注册内置工具（echo、current_utc_time），更多工具也在这里加

    llm_client = LLMClientFactory.create(
        provider=settings.llm.provider,
        model=settings.llm.model,
        temperature=settings.llm.temperature,
        max_tokens=settings.llm.max_tokens,
    )  # 💡 学习提示：工厂在这里检查 API Key 是否存在，缺少时立刻报错（"快速失败"原则）

    # 💡 学习提示：ReActAgent 使用"依赖注入"——把所有组件作为参数传进去，
    # 而不是在 ReActAgent 内部自己创建。这样可以在测试时注入 mock 对象
    return ReActAgent(
        llm_client=llm_client,
        prompt_assembler=ContextAssembler(),
        tool_registry=registry,
        memory=WorkingMemory(max_tokens=settings.memory.working_max_tokens),
        max_steps=settings.agent.max_steps,
    )


@app.command()
# 💡 学习提示：@app.command() 把这个函数注册为一个 CLI 子命令。
# 函数名 chat → 命令名 chat，用户输入 `athena chat "问题"` 就会调用这里。
# Typer 自动把函数参数转换为命令行参数，类型注解用来校验输入。
def chat(
    query: str = typer.Argument(..., help="Single-turn user query."),
    # 💡 学习提示：typer.Argument(...) 中的 ... 表示"必填参数"（Ellipsis 是 Python 的省略号对象）
    # 用户不传 query 时，Typer 会自动报错并显示用法帮助
    config: Path | None = typer.Option(None, "--config", "-c", help="Path to config.yaml."),
    # 💡 学习提示：typer.Option 是"可选参数"，用 --config 或 -c 传入
    # 默认 None 表示使用默认配置文件路径（config.yaml）
) -> None:
    """
    执行单次 Agent 对话请求（非交互模式）。

    功能说明：
        接收一个问题，运行 Agent，输出答案，然后退出。
        适合脚本调用、管道传递或快速验证：
        `athena chat "北京今天天气怎么样？"`
        `echo "计算1+1" | athena chat -`

    参数说明：
        query:  用户的问题（命令行位置参数，必填）
        config: 配置文件路径（可选，--config 或 -c 指定）

    返回值：
        无（结果通过 typer.echo() 打印到终端）

    异常处理：
        AthenaError → 红色显示错误信息，以退出码 1 结束进程
        （退出码 1 是 Unix 约定的"程序出错"信号，CI/CD 系统可以据此判断是否成功）

    使用示例：
        $ athena chat "帮我写一首关于春天的诗"
        $ athena chat "计算123*456" --config ./my_config.yaml
    """
    """
    🔍 原理讲解：为什么需要 asyncio.run()？

    CLI 的 main 线程是同步的（普通 Python 执行），没有事件循环。
    但 agent.run() 是 async 函数，必须在事件循环里运行。

    asyncio.run() 做了三件事：
    1. 创建一个新的事件循环
    2. 在循环里运行 agent.run(query)，等它完成
    3. 关闭并清理事件循环

    同步世界 → asyncio.run() 桥接 → 异步世界
    CLI chat()  →  asyncio.run()  →  agent.run()

    注意：每次调用 chat 命令都会创建新的事件循环，适合单次请求。
    start() 命令的 while 循环里也是每轮都调用 asyncio.run()，
    这会反复创建销毁事件循环，是 MVP 的简化做法。
    """
    try:
        agent = build_agent(config)
        response = asyncio.run(agent.run(query))  # 💡 学习提示：同步入口进入异步世界的关键一步
        typer.echo(response.answer)  # 💡 学习提示：typer.echo() 类似 print()，但 Typer 推荐用它，方便测试时捕获输出
    except AthenaError as exc:
        # 💡 学习提示：typer.secho() = styled echo，fg=RED 让错误信息显示为红色，方便用户区分
        typer.secho(f"Athena error [{exc.code}]: {exc.message}", fg=typer.colors.RED)
        # 💡 学习提示：raise typer.Exit(code=1) 终止程序并返回退出码 1。
        # 退出码 0 = 成功，非 0 = 失败（Unix 约定）。
        # Shell 脚本和 CI/CD 流水线通过退出码判断命令是否成功。
        # from exc 保留原始异常调用栈，方便调试。
        raise typer.Exit(code=1) from exc


@app.command()
def start() -> None:
    """
    启动交互式 MVP 对话会话（交互模式）。

    功能说明：
        进入持续对话循环——用户输入问题，Agent 回答，然后等待下一个问题。
        输入 "exit" 或 "quit" 退出会话。
        在同一次 start 会话里，Agent 的 WorkingMemory 是持续存在的，
        能"记住"本次会话的历史对话（但重启后丢失）。

    设计思路：
        "REPL"（Read-Eval-Print Loop）模式——终端里的交互循环：
        ① Read（读）：typer.prompt("You") 等待用户输入
        ② Eval（执行）：asyncio.run(agent.run(query)) 处理问题
        ③ Print（打印）：typer.echo(f"Athena: {response.answer}") 显示结果
        ④ Loop（循环）：回到第 ①步

        注意：Agent 在循环外创建（build_agent() 只调用一次），
        这样同一次会话共享同一个 WorkingMemory，实现多轮对话记忆。

    使用示例：
        $ athena start
        Athena Agent MVP session. Type 'exit' to quit.
        You: 你好
        Athena: 你好！有什么可以帮你？
        You: 帮我计算 100+200
        Athena: 100 + 200 = 300
        You: exit
        $
    """
    try:
        # 💡 学习提示：Agent 在循环外创建，整个会话共用同一个实例。
        # 这意味着 WorkingMemory 会跨轮次累积，AI 能记住前几轮的问答。
        # 如果放在 while 循环里，每轮都重建 Agent，就没有记忆了。
        agent = build_agent(None)
    except AthenaError as exc:
        # 💡 学习提示：初始化阶段（如 API Key 缺失）的错误在进入循环前就处理，
        # 避免用户开始打字后才收到初始化错误的困惑体验
        typer.secho(f"Athena error [{exc.code}]: {exc.message}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    typer.echo("Athena Agent MVP session. Type 'exit' to quit.")

    # 💡 学习提示：while True 是 REPL 的标准写法，靠内部 break 退出，而不是 while 条件。
    # 这比 while user_input != "exit" 更清晰，因为退出条件可能有多个（exit、quit、Ctrl+C）
    while True:
        query = typer.prompt("You")  # 💡 学习提示：typer.prompt() 显示提示符并等待用户输入（阻塞），类似 input() 但有更好的格式支持
        if query.strip().lower() in {"exit", "quit"}:
            # 💡 学习提示：.lower() 让退出命令大小写不敏感（"Exit"、"EXIT"、"exit" 都能退出）
            # 用集合 {"exit", "quit"} 而不是 or 语句，便于将来添加更多退出词
            break
        try:
            response = asyncio.run(agent.run(query))
            typer.echo(f"Athena: {response.answer}")
        except AthenaError as exc:
            # 💡 学习提示：循环里的错误只打印警告，不终止会话（不 raise typer.Exit）。
            # 这样一次工具调用失败或网络超时，不会让整个交互会话崩溃，
            # 用户可以继续提问。这是"容错交互"的体验设计。
            typer.secho(f"Athena error [{exc.code}]: {exc.message}", fg=typer.colors.RED)


def main() -> None:
    """
    程序入口——启动 Typer CLI 应用。

    功能说明：
        这是 setup.py 中 entry_points 指向的函数，
        用户安装包后输入 `athena` 命令，就会执行这里的 app()。

    设计思路：
        把 app() 包装在 main() 里，而不是直接暴露 app：
        ① setup.py 的 entry_points 只能指向函数，main() 提供了这个函数
        ② 将来需要在 app() 前后做初始化/清理时，可以在 main() 里加代码
        ③ 代码风格统一：所有 Python 程序都有 main() 的习惯

    使用示例：
        # 安装后通过命令行使用：
        $ athena --help
        $ athena chat "你好"
        $ athena start
    """
    app()


# 💡 学习提示：if __name__ == "__main__" 是 Python 的标准模式。
# 当直接运行这个文件时（python main.py），__name__ 等于 "__main__"，会执行 main()。
# 当这个文件被其他模块 import 时，__name__ 等于模块路径，不会自动执行 main()。
# 这样既能作为模块被导入，也能作为脚本直接运行，两者不冲突。
if __name__ == "__main__":
    main()


"""
🤔 思考题（结合 CLI 和整体架构一起思考）：

1. asyncio.run() 的反复创建问题：
   start() 命令的 while 循环里每轮都调用 asyncio.run()，
   这会反复创建和销毁事件循环（有一定开销）。
   你能想到什么办法，在整个 start 会话里只创建一次事件循环？
   提示：考虑 asyncio.get_event_loop().run_until_complete() 或把整个 while 循环改成 async。

2. start() 的记忆设计：
   当前 start() 里 Agent 在循环外创建，所有轮次共享 WorkingMemory。
   如果想让用户能说"重置对话，重新开始"，你会怎么实现？
   需要修改哪个文件？

3. 错误处理策略差异：
   chat() 里遇到 AthenaError 会 raise typer.Exit(code=1) 终止程序；
   start() 的 while 循环里遇到 AthenaError 只打印不终止。
   你觉得这两种策略各自合理吗？什么场景下 start() 也应该终止？
   提示：想想 API Key 失效、网络断开、磁盘满了等情况。

4. 新增命令的步骤：
   假设要添加一个 `athena history` 命令，显示当前会话的对话历史，
   你需要修改哪些地方？有什么挑战？
   （提示：WorkingMemory 目前不是持久化的，每次 build_agent 都是全新的记忆）

5. （选做）Web API 化：
   如果要把 Athena Agent 改造成一个 HTTP API（用 FastAPI 提供 /chat 接口），
   你觉得现有的代码架构支持这个改造吗？
   需要新建什么文件？build_agent() 能复用吗？
   asyncio.run() 还需要吗？（FastAPI 本身就是异步的）
"""
