"""
📦 模块名称：工具注册表（Tool Registry）
📍 架构位置：工具层（Tool Layer）—— Agent 执行器的"右手"，负责对外调用能力：
           [ReActAgent] → 决定调用哪个工具 → 【本文件 ToolRegistry.invoke()】 → 执行工具函数
🎯 核心作用：管理 Agent 能使用的所有工具，提供"注册 → 描述 → 调用"的完整生命周期
🔗 依赖关系：
   - 依赖：inspect（Python 标准库，用于反射读取函数签名）、asyncio
   - 被依赖：
     * athena/agent/executor.py   → 调用 invoke() 执行工具、调用 describe_tools() 生成工具列表
     * athena/prompt/assembler.py → 调用 describe_tools() 把工具注入提示词
     * athena/tools/builtin/basic.py → 调用 registry.register 装饰器注册内置工具
💡 设计思路：
   采用"装饰器注册模式"（Decorator-based Registry Pattern）：
   ① 开发者用 @registry.register 装饰一个普通函数，它就变成了 Agent 可用的工具
   ② ToolRegistry 自动读取函数名、文档注释、参数类型作为工具描述，零手动配置
   ③ 调用时返回 ToolResult（成功/失败封装），而不是抛出异常，让 Agent 能优雅处理失败
   
   同时支持同步和异步工具函数，通过 inspect.isawaitable() 运行时检测自动适配。
📚 学习重点：
   1. 装饰器注册模式——@registry.register 的工作原理
   2. inspect.signature()——Python 如何在运行时"读取"函数的参数信息
   3. ToolResult 的"结果类型"设计——为什么不抛异常而是返回 success/error
   4. 同步/异步工具的统一调用机制（invoke() 里的 isawaitable 判断）
   5. frozen=True 在三个数据类上的含义
"""

from __future__ import annotations  # 💡 学习提示：支持类型注解前向引用，全项目统一风格

import asyncio
import inspect   # 💡 学习提示：Python 标准库的"反射"工具，可以在运行时检查函数的参数、文档等信息
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import TypeAlias, cast

from athena.exceptions import ErrorCode
from athena.types import JSONValue
logger = logging.getLogger(__name__)

# 💡 学习提示：TypeAlias 定义了一个"类型别名"——ToolHandler 是一个简短的名字，
# 代表"接受任意参数、返回 JSONValue 或 Awaitable[JSONValue] 的函数"。
# 不用 TypeAlias 也行，但名字太长写起来累，别名让类型注解更可读。
# Callable[..., JSONValue | Awaitable[JSONValue]] 的含义：
# - Callable[...]  → 可调用的函数
# - JSONValue      → 同步函数直接返回
# - Awaitable[...] → 异步函数返回 coroutine（需要 await）
ToolHandler: TypeAlias = Callable[..., JSONValue | Awaitable[JSONValue]]


# ============================================================
# 📌 数据模型层：三个核心数据结构
# ============================================================


@dataclass(frozen=True)
class Tool:
    """
    已注册工具的元数据（不可变快照）。

    功能说明：
        描述一个工具"是什么"——它叫什么名字、怎么用、需要什么参数、实际函数在哪里。
        就像字典里的词条：有词（name）、有释义（description）、有用法（parameters）。

    字段说明：
        name:                工具的唯一标识符，来自函数名（func.__name__）
                             示例："calculator"、"echo"、"current_utc_time"

        description:         工具的功能说明，来自函数的文档注释（docstring）
                             这段文字会被注入进提示词，让 AI 知道这个工具能做什么！
                             所以工具函数的 docstring 要写清楚，AI 根据它决定是否调用该工具。

        parameters:          参数名 → 类型名的字典，从函数签名自动提取
                             示例：{"expression": "<class 'str'>"}

        required_parameters: 没有默认值的必填参数名，用 tuple 存储
                             invoke() 用这个字段检查 Agent 是否漏传了参数

        handler:             指向原始函数的引用，invoke() 时实际调用它

    设计思路：
        frozen=True 让 Tool 不可变——一旦注册就无法修改任何字段。
        不可变对象更安全：不用担心某段代码悄悄修改了工具的描述或处理函数，
        也不需要加锁（线程安全）。
    """

    name: str
    description: str
    parameters: Mapping[str, str]
    required_parameters: tuple[str, ...]  # 💡 学习提示：用 tuple 而不是 list，因为 frozen dataclass 的字段必须是不可变类型
    handler: ToolHandler


@dataclass(frozen=True)
class ToolCall:
    """
    Agent 发出的一次工具调用请求（不可变）。

    功能说明：
        封装"我要调用哪个工具、传什么参数"这两个信息。
        由 executor.py 根据 LLM 的决策创建，传给 ToolRegistry.invoke()。

    字段说明：
        name:      要调用的工具名称（对应 Tool.name）
                   示例："calculator"

        arguments: 传给工具的参数字典
                   示例：{"expression": "1234 * 5678"}

    设计思路：
        专门用一个对象封装调用请求，而不是直接传 name 和 arguments 两个参数，
        好处是：方便日志记录、方便未来扩展（如加 timeout、caller_id 等字段），
        符合"将数据打包为对象"的面向对象思想。

    使用示例：
        call = ToolCall(name="calculator", arguments={"expression": "1+1"})
        result = await registry.invoke(call)
    """

    name: str
    # 💡 学习提示：frozen dataclass 里的可变字段（dict）也要用 field(default_factory=dict)
    # 原因和 Pydantic 一样：避免所有实例共享同一个默认字典对象
    arguments: dict[str, JSONValue] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolResult:
    """
    工具执行结果的标准化封装（结果类型模式）。

    功能说明：
        无论工具成功还是失败，都用这个对象返回，而不是抛出异常。
        executor.py 检查 result.success 来决定下一步：
        - 成功：把 content 作为 Observation 告诉 AI
        - 失败：把 error 作为 Observation 告诉 AI（"这个工具失败了，换个办法"）

    字段说明：
        success: 工具是否成功执行（True/False）
        content: 工具的返回内容（成功时有值，失败时为空字符串）
        error:   失败时的错误描述（成功时为 None）

    设计思路：
        "结果类型"（Result Type）模式，又叫"铁路导向编程"（Railway Oriented Programming）。
        两条轨道：成功轨道（success=True, content=...）和失败轨道（success=False, error=...）。

    # 🎯 面试考点：为什么用 success/error 字段而不是直接抛异常？
    # 答：Agent 需要把工具失败的信息传递给 AI，让 AI 知道"这个工具不可用，请换个思路"。
    # 如果抛异常，就需要在 executor.py 的循环里 try/except，逻辑更复杂。
    # 用 ToolResult 把成功和失败统一为一种返回形式，executor.py 只需 if result.success 判断，
    # 更简洁，也更像函数式编程的风格。

    使用示例：
        # 成功结果：
        result = ToolResult(success=True, content="7006652")

        # 失败结果：
        result = ToolResult(success=False, content="", error="除零错误")

        # executor.py 里的使用方式：
        observation = result.content if result.success else str(result.error)
    """

    success: bool
    content: str
    error: str | None = None  # 💡 学习提示：error 默认为 None，成功时不需要赋值，减少调用时的样板代码


# ============================================================
# 📌 核心实现层：工具注册表
# ============================================================

class ToolRegistry:
    """
    工具注册表——管理所有 Agent 可用工具的生命周期。

    功能说明：
        提供三项核心能力：
        ① register()      → 把一个普通 Python 函数注册为 Agent 工具（装饰器方式）
        ② describe_tools() → 生成所有工具的可读描述，注入进提示词让 AI 知道能用什么工具
        ③ invoke()         → 根据工具名和参数，安全地执行对应的工具函数

    设计思路：
        "注册表模式"（Registry Pattern）——维护一个 name → Tool 的字典，
        注册时存入，调用时按名查找。
        类似 Flask 的路由表：app.route("/") 把 URL 映射到函数；
        这里 registry.register 把工具名映射到处理函数。

    # 🎯 面试考点：为什么 ToolRegistry 用普通 class 而不是 dataclass？
    # 答：dataclass 适合"纯数据"对象（只有字段，方法很少）。
    # ToolRegistry 主要是行为（三个方法），不是数据。
    # 更重要的是，tools 字典需要在运行时动态修改（注册新工具），
    # 而 frozen dataclass 不允许修改，普通 class 更适合。

    使用示例：
        registry = ToolRegistry()

        @registry.register
        def calculator(expression: str) -> str:
            \"\"\"Evaluate a simple math expression.\"\"\"
            return str(eval(expression))  # 注意：生产环境不要用 eval！

        # 现在 registry 里有一个叫 "calculator" 的工具
        call = ToolCall(name="calculator", arguments={"expression": "1+1"})
        result = await registry.invoke(call)
        print(result.content)  # "2"
    """

    def __init__(self) -> None:
        """
        初始化一个空的工具注册表。

        设计思路：
            tools 字典是注册表的核心数据结构，key 是工具名，value 是 Tool 对象。
            初始为空，工具通过 register() 方法动态添加。
        """
        # 💡 学习提示：用 dict[str, Tool] 而不是 list[Tool]，
        # 目的是 O(1) 的按名查找——invoke() 时直接 self.tools[name]，无需遍历
        self.tools: dict[str, Tool] = {}

    def register(self, func: ToolHandler) -> ToolHandler:
        """
        将一个 Python 函数注册为 Agent 工具（装饰器）。

        功能说明：
            通过 Python 的反射机制（inspect 模块）自动提取函数的：
            - 名字（func.__name__） → 工具的唯一标识
            - 文档注释（docstring） → 工具描述（会注入进提示词，AI 根据此决定是否调用）
            - 参数和类型（signature）→ 参数列表（AI 填写时的参考）
            然后创建 Tool 对象存入 self.tools 字典。

        参数说明：
            func: 要注册的函数，可以是同步函数或 async 函数

        返回值：
            原始函数本身（不包装，不修改）

        设计思路：
            "装饰器注册"模式——返回原始函数不做任何包装，
            这样被装饰的函数在注册后还能当普通函数直接调用，
            不影响单元测试和其他直接使用场景。

        # 🎯 面试考点：为什么 register() 返回原始函数 func 而不是包装后的新函数？
        # 答：装饰器的常见模式是返回包装函数（wrapper），但这里目的只是"记录"，
        # 不需要改变函数行为。返回原函数：
        # ① 原函数依然可以独立调用（方便测试）
        # ② 函数的 __name__、__doc__ 等属性不变（调试友好）
        # ③ 不引入额外的调用栈层次（性能略好）

        使用示例：
            # 方式一：装饰器语法（最常用）
            @registry.register
            def echo(text: str) -> str:
                \"\"\"Return the text unchanged.\"\"\"
                return text

            # 方式二：等价的直接调用
            registry.register(echo)
        """
        """
        🔍 原理讲解：inspect.signature() 如何工作？

        inspect.signature(func) 返回函数的"签名"对象，包含所有参数信息。

        举个例子，对于函数：
            def calculator(expression: str, precision: int = 2) -> str:
                ...

        signature.parameters 是一个有序字典：
        {
            "expression": Parameter(name="expression", annotation=str, default=Parameter.empty),
            "precision":  Parameter(name="precision",  annotation=int, default=2)
        }

        通过遍历这个字典，我们可以：
        - 提取参数名和类型 → parameters = {"expression": "str", "precision": "int"}
        - 判断是否必填：default is Parameter.empty → required = ("expression",)
            （因为 expression 没有默认值，precision 有默认值 2）
        """
        signature = inspect.signature(func)
        # 💡 学习提示：str(parameter.annotation) 把类型注解转成字符串。
        # 如果函数没写类型注解，annotation 是 inspect.Parameter.empty，
        # 转成字符串是 "<class 'inspect._empty'>"，虽然不优雅但不会报错。
        parameters = {
            name: str(parameter.annotation)
            for name, parameter in signature.parameters.items()
        }
        # 💡 学习提示：parameter.default is inspect.Parameter.empty 判断"没有默认值"。
        # 注意必须用 is 而不是 ==，因为 empty 是单例哨兵对象，用 == 可能被误触发。
        required_parameters = tuple(
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
        )
        tool = Tool(
            name=func.__name__,   # 💡 学习提示：函数名自动成为工具名，所以工具函数命名要有意义
            description=inspect.getdoc(func) or "",  # 💡 学习提示：getdoc() 比 func.__doc__ 更好——会自动去除缩进和首尾空白
            parameters=parameters,
            required_parameters=required_parameters,
            handler=func,  # 💡 学习提示：存函数引用而不是函数调用结果，invoke() 时才真正执行
        )
        self.tools[tool.name] = tool
        logger.debug("Registered tool: %s", tool.name)
        return func  # 💡 学习提示：返回原始函数，保持装饰器透明性（不改变函数行为）

    def describe_tools(self) -> str:
        """
        生成所有已注册工具的可读描述，用于注入提示词。

        功能说明：
            把工具注册表里的所有工具，渲染成 AI 能理解的文字描述，
            插入提示词的 {tools} 槽位，让 AI 知道"我有哪些工具可以用"。

        返回值：
            多行字符串，每行描述一个工具，格式为：
            "- 工具名(参数名: 类型, ...): 描述"
            示例：
            "- calculator(expression: <class 'str'>): Evaluate a math expression"
            "- echo(text: <class 'str'>): Return the text unchanged"
            没有工具时返回 "No tools are available."

        设计思路：
            格式对 AI 的理解至关重要——工具描述太模糊，AI 就不知道什么时候调用；
            描述太长，又浪费 Token。这里用简洁的"- name(params): description"格式，
            足够让 AI 判断该不该用这个工具，以及怎么填参数。

        # ⚡ 优化建议：参数类型显示的是 "<class 'str'>"（Python 内部格式），对 AI 不友好。
        # 可以在 register() 时做处理：str(annotation) → annotation.__name__（如 "str"）
        # 让工具描述更简洁：calculator(expression: str) 而不是 calculator(expression: <class 'str'>)
        """
        if not self.tools:
            return "No tools are available."  # 💡 学习提示：提前返回，避免空列表时 join 出空字符串
        lines: list[str] = []
        for tool in self.tools.values():
            # 💡 学习提示：", ".join() 把参数列表拼成 "name: type, name: type" 格式
            params = ", ".join(f"{name}: {type_name}" for name, type_name in tool.parameters.items())
            lines.append(f"- {tool.name}({params}): {tool.description}")
        return "\n".join(lines)

    async def invoke(self, call: ToolCall) -> ToolResult:
        """
        安全地执行一个工具调用，统一返回 ToolResult（不抛异常）。

        功能说明：
            完整流程：查找工具 → 检查参数 → 执行函数 → 封装结果。
            每个环节的失败都被捕获，转换为 ToolResult(success=False, error=...)，
            而不是让异常冒泡到 executor.py。

        参数说明：
            call: 包含工具名和参数的调用请求

        返回值：
            ToolResult，无论成功或失败都有值（不会抛出异常）

        设计思路：
            "永不失败"的公共接口——invoke() 承诺总是返回 ToolResult，
            调用方（executor.py）不需要 try/except，只需 if result.success 判断。
            这简化了 ReAct 循环的逻辑，工具失败变成了一个"可观察的事件"，
            而不是打断整个循环的异常。
        """
        """
        🔍 原理讲解：invoke() 的四道防线

        第一道：工具存在性检查
            self.tools.get(call.name) 返回 None → 工具不存在
            → ToolResult(success=False, error="Tool not found: xxx")

        第二道：必填参数检查
            遍历 tool.required_parameters，检查哪些在 call.arguments 里找不到
            → ToolResult(success=False, error="Missing required parameter(s): xxx")

        第三道：执行函数（同步/异步双路支持）
            判断调用结果是否可 await → 二选一执行路径（见下方详细注释）

        第四道：执行时异常捕获
            任何运行时异常（除零、超时等）→ ToolResult(success=False, error=...)
        """
        tool = self.tools.get(call.name)
        if tool is None:
            # 💡 学习提示：工具不存在时返回失败结果而不是抛 KeyError，
            # 让 AI 知道"这个工具不存在，换个工具或换个方法"
            return ToolResult(
                success=False,
                content="",
                error=f"Tool not found: {call.name}",
            )
        # 💡 学习提示：列表推导式找出所有"在必填参数列表里但不在调用参数里"的参数名
        # required_parameters 来自函数签名的无默认值参数，确保 AI 不会漏传关键参数
        missing_parameters = [
            parameter
            for parameter in tool.required_parameters
            if parameter not in call.arguments
        ]
        if missing_parameters:
            return ToolResult(
                success=False,
                content="",
                error=(
                    f"Missing required parameter(s) for tool '{call.name}': "
                    f"{', '.join(missing_parameters)}"
                ),
            )
        try:
            # 💡 学习提示：**call.arguments 是"字典解包"——把字典展开为关键字参数。
            # 等价于 tool.handler(expression="1+1") 当 arguments={"expression": "1+1"}
            result = tool.handler(**call.arguments)
            if inspect.isawaitable(result):
                # 情况 A：工具是 async 函数，返回了 coroutine，需要 await
                # 示例：async def fetch_weather(city: str) -> str: ...
                value = await cast(Awaitable[JSONValue], result)
            else:
                # 情况 B：工具是普通同步函数，result 已经是计算结果（不是 coroutine）
                # 💡 学习提示：asyncio.to_thread(lambda: result) 这里看起来有些奇怪——
                # result 已经是计算好的值了，为什么还要放进线程？
                # 原因是统一返回 Awaitable，让下面的 await 语法总能工作。
                # 代价：同步工具函数已经在事件循环里跑完了（如果耗时会短暂阻塞），
                # to_thread 在这里只是为了语法统一，并非真正的"移到线程"。
                # ⚡ 优化建议：若要真正避免同步工具阻塞事件循环，应改为：
                # value = await asyncio.to_thread(tool.handler, **call.arguments)
                # 即：在调用之前就把函数扔到线程池，而不是调用之后再包装结果。
                value = await asyncio.to_thread(lambda: result)
            return ToolResult(success=True, content=str(value))
        except Exception as exc:
            # 💡 学习提示：宽泛的 except Exception 捕获所有运行时异常，
            # 转换为 ToolResult 失败结果，防止一个工具的 bug 导致整个 Agent 崩溃。
            # logger.exception() 会把完整堆栈写入日志，方便事后调试。
            logger.exception("Tool execution failed: %s", call.name)
            return ToolResult(
                success=False,
                content="",
                error=f"{ErrorCode.TOOL_EXECUTION_FAILED}: {exc}",
            )


"""
🤔 思考题（结合 ToolRegistry 深入思考）：

1. 工具的安全性边界：
   现在工具函数可以是任意 Python 函数，包括 os.system("rm -rf /")。
   在真实生产环境中，你会如何限制工具的能力范围？
   提示：考虑沙箱执行、允许列表（allowlist）机制、工具权限分级。

2. 参数类型校验：
   当前 invoke() 只检查"必填参数是否存在"，不检查参数类型是否正确。
   如果 AI 给 calculator 传了 expression=123（数字而不是字符串），工具函数会怎样？
   你会在哪里加类型校验？加在 invoke() 里还是工具函数内部？

3. 工具描述的质量影响：
   AI 是否调用某个工具，完全取决于 docstring 里的描述。
   如果一个工具的 docstring 写得很模糊，AI 会怎么表现？
   你能想到什么方法来"测试"工具描述的质量？

4. 同名工具的覆盖：
   当前 register() 会直接覆盖同名工具（self.tools[tool.name] = tool）。
   这在某些场景下可能是 bug（不小心注册了两个同名工具，第二个覆盖了第一个）。
   你会加什么保护机制？是报错、是警告、还是允许覆盖但记录日志？

5. （选做）工具超时控制：
   如果一个工具调用了慢 API（如网络请求），可能让 Agent 等很久。
   你会如何给 invoke() 加超时控制？
   提示：asyncio.wait_for(coroutine, timeout=5.0) 是一个思路。
"""
