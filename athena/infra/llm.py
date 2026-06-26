"""
📦 模块名称：LLM 网关抽象层（LLM Gateway）
📍 架构位置：基础设施层（Infrastructure Layer）—— 架构的最底层，负责与外部 AI 服务对话
🎯 核心作用：把"调用大模型"这件事封装成统一接口，让上层代码不用关心用的是 OpenAI 还是 Claude
🔗 依赖关系：
   - 依赖：litellm（第三方 LLM 路由库）、athena.exceptions（统一错误处理）
   - 被依赖：athena/agent/executor.py（Agent 执行器通过此模块向大模型发送消息）
💡 设计思路：
   使用"Protocol（协议接口）+ 工厂模式"的经典组合：
   ① Protocol 定义接口 → 上层代码只依赖接口，不依赖具体实现（依赖倒置原则 DIP）
   ② Factory 统一创建 → 调用方不需要知道如何构造 LiteLLMClient，也不会遗漏初始化步骤
   ③ LiteLLM 作为统一路由 → 一个库能调用 100+ 个模型，省去逐一集成的麻烦
📚 学习重点：
   1. Python Protocol 是什么？和 ABC 抽象基类有什么区别？（见 LLMClient 类）
   2. asyncio.to_thread() 为什么要用？同步阻塞代码和异步事件循环如何共存？（见 complete 方法）
   3. 工厂模式（Factory Pattern）在真实项目中的落地写法（见 LLMClientFactory）
   4. 日志脱敏——为什么要清洗错误信息？（见 _sanitize_error_message）
"""

from __future__ import (  # 💡 学习提示：允许在类型注解里引用还未定义的类（如自引用），避免循环导入。Python 3.10+ 之后默认支持，但加上这行兼容性更好
    annotations,
)

import asyncio
import logging
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from athena.exceptions import ErrorCode, LLMError

logger = logging.getLogger(__name__)  # 💡 学习提示：每个模块用自己的名字创建 logger。
# 好处：日志输出里会带上模块路径（如 "athena.infra.llm"），出了 bug 一眼就能定位到是哪个文件打的日志。
# __name__ 是 Python 内置变量，值就是当前模块的完整包路径。


# ============================================================
# 📌 数据模型层：定义"数据长什么样"
# ============================================================


class LLMMessage(BaseModel):
    """
    表示发给大模型的一条聊天消息。

    功能说明：
        就像微信消息一样，每条消息有"谁发的（role）"和"说了什么（content）"。
        大模型 API 用 role-content 格式区分系统指令、用户输入和 AI 历史回复。

    参数说明：
        role:    消息角色，常见值：
                 - "system"    → 系统提示词（给 AI 的行为指令，如"你是一个助手"）
                 - "user"      → 用户说的话
                 - "assistant" → AI 之前的回复（放入历史实现多轮对话）
        content: 消息的具体文字内容

    设计思路：
        继承 Pydantic BaseModel 做自动类型校验。如果你不小心传了 role=123（数字），
        Pydantic 会在创建时立刻报错，而不是等调用 API 才发现。

    使用示例：
        msg = LLMMessage(role="user", content="你好，帮我写一首诗")
        msg.model_dump()  # → {"role": "user", "content": "你好，帮我写一首诗"}
    """

    role: str
    content: str


class LLMResponse(BaseModel):
    """
    大模型返回结果的标准化格式。

    功能说明：
        不同的大模型 API（OpenAI、Claude、Deepseek）返回的数据结构各不相同。
        这个类把它们"翻译"成统一格式，让上层代码只用看一种结构。
        就像电源适配器——不管什么国家的插头，统一转成标准接口。

    参数说明：
        content: AI 实际回复的文字（最重要的部分）
        model:   实际使用的模型名称（用于日志追踪和成本分析）
        usage:   Token 用量统计字典，包含：
                 - "prompt_tokens"     → 输入消耗的 token 数
                 - "completion_tokens" → 输出消耗的 token 数
                 - "total_tokens"      → 总计（约 1 token ≈ 0.75 英文单词 / 1.5 汉字）

    设计思路：
        usage 用 Mapping[str, int] 而非固定字段，是因为不同模型返回的字段名可能不同
        （有的叫 input_tokens，有的叫 prompt_tokens），字典更灵活。

    使用示例：
        resp = LLMResponse(content="今天天气不错", model="gpt-4o",
                           usage={"prompt_tokens": 10, "total_tokens": 15})
        print(resp.content)  # "今天天气不错"
    """

    content: str
    model: str
    # 💡 学习提示：default_factory=dict 而不是 default={}
    # 经典 Python 坑：可变默认值（如 {}）在所有实例间共享同一个字典对象！
    # 比如 a = LLMResponse(...); b = LLMResponse(...); a.usage["x"] = 1 → b.usage 也被改了！
    # default_factory 每次创建实例时都新建一个字典，彻底规避这个问题。
    usage: Mapping[str, int] = Field(default_factory=dict)


# ============================================================
# 📌 接口层：定义"能做什么"（Protocol 鸭子类型接口）
# ============================================================


class LLMClient(Protocol):
    """
    LLM 客户端的"接口契约"（Protocol）。

    功能说明：
        定义了所有 LLM 客户端必须提供的方法。任何实现了 complete() 方法的类，
        都自动满足这个协议，不需要显式继承（这就是"鸭子类型"）。

    设计思路：
        Python Protocol 实现"结构化子类型"：
        "如果一个东西走起来像鸭子、叫起来像鸭子，那它就是鸭子。"
        只要一个类有正确签名的 complete() 方法，就算是合法的 LLMClient，
        无需写 class MyClient(LLMClient)。

    使用示例：
        def run_step(client: LLMClient) -> None:  # 只依赖接口，不关心具体实现
            response = await client.complete(messages)
    """

    # 🎯 面试考点：为什么用 Protocol 而不是 ABC（Abstract Base Class）抽象基类？
    # 答：
    # Protocol（结构化）：只看"有没有这个方法"，第三方库不用改源码就能满足你的接口。
    # ABC（名义化）：必须显式写 class MyClient(LLMClient)，更明确但耦合更强。
    # 在 Agent 框架里用 Protocol 的好处：未来可以接入任意第三方 LLM 客户端库，
    # 只要它有 complete() 方法就能直接用，无需包装或继承。
    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """Return a completion for the provided chat messages."""


# ============================================================
# 📌 实现层：真正干活的类
# ============================================================


class LiteLLMClient(BaseModel):
    """
    基于 LiteLLM 库的 LLM 客户端具体实现。

    功能说明：
        LiteLLM 是一个"万能转接头"库，支持 OpenAI、Claude、Gemini、Deepseek 等
        100+ 个模型，只需改模型名字就能切换，不用改任何调用代码。
        这个类把 LiteLLM 的同步阻塞调用包装成 Athena 异步接口。

    参数说明：
        model:       模型标识符，如 "gpt-4o"、"claude-3-5-sonnet-20241022"、"deepseek/deepseek-chat"
        temperature: 创造性/随机性控制（0.0~2.0）：
                     0.0 → 每次结果几乎一样（适合代码生成、结构化输出）
                     0.7 → 平衡创造性与稳定性（适合对话）
                     2.0 → 非常随机发散（适合头脑风暴）
        max_tokens:  最多生成多少 token（1 token ≈ 0.75 英文单词 / 1.5 汉字）

    设计思路：
        继承 Pydantic BaseModel 而不是普通 class，好处是：
        ① 参数自动校验（max_tokens 是 PositiveInt，传 0 或负数会报错）
        ② 可序列化（model_dump() 方便存日志和配置）
        ③ 与整个 Athena 的配置风格统一

    使用示例：
        client = LiteLLMClient(model="gpt-4o", temperature=0.2, max_tokens=1024)
        response = await client.complete([LLMMessage(role="user", content="你好")])
        print(response.content)  # AI 的回复文字
    """

    # 💡 学习提示：arbitrary_types_allowed=True 告诉 Pydantic 允许字段使用非 Pydantic 原生类型。
    # 这里主要是为了后续扩展（如果要加 httpx.AsyncClient 这类外部对象作为字段）。
    # 现有字段（str、float、PositiveInt）本身不需要这个，但加上是良好的前瞻实践。
    model_config = ConfigDict(arbitrary_types_allowed=True)

    model: str
    temperature: float = 0.2
    # 💡 学习提示：PositiveInt 是 Pydantic 提供的约束类型，自动保证值 > 0。
    # 比手写 if max_tokens <= 0: raise ValueError(...) 更简洁，且错误信息更标准。
    max_tokens: PositiveInt = 1024

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """
        异步地调用 LLM 获取回复（对外暴露的主接口）。

        功能说明：
            接收消息列表，返回 AI 回复。关键细节：这个方法是 async 的，
            但 LiteLLM 底层是同步阻塞调用，需要特殊桥接避免卡住事件循环。

        参数说明：
            messages: 有序的聊天消息列表（包含历史对话，让 AI 理解上下文）

        返回值：
            LLMResponse 对象，包含 AI 的回复文字、模型名和 token 用量

        异常：
            LLMError: 网络失败、API Key 错误、模型不存在等情况都会抛出此异常

        设计思路：
            用 try/except 统一捕获所有底层异常，转换为 Athena 自己的 LLMError。
            这叫"异常转译"——上层代码只需处理 LLMError，无需知道 litellm 内部抛了什么。

        使用示例：
            resp = await client.complete([LLMMessage(role="user", content="1+1=?")])
            print(resp.content)  # "2"
        """
        try:
            # 💡 学习提示：asyncio.to_thread() 是连接"同步世界"和"异步世界"的桥梁。
            #
            # 背景：Python 的 async/await 基于"事件循环"——一个线程轮流执行各个协程。
            # 如果在事件循环里直接调用同步阻塞代码（如网络请求），会卡住整个循环，
            # 导致所有其他协程都无法运行（就像一辆车横在路口堵死整条街）。
            #
            # to_thread() 的做法：把同步函数丢到一个独立的线程池线程里运行，
            # 主事件循环继续转，等线程跑完再把结果拿回来。
            return await asyncio.to_thread(self._complete_sync, messages)
        except Exception as exc:
            # 💡 学习提示：先清洗错误信息，防止 API Key 等敏感数据泄露到日志文件里
            message = _sanitize_error_message(str(exc))
            logger.error("LLM completion failed: %s", message)
            # 💡 学习提示：raise ... from exc 保留了原始异常的调用栈（cause chain）。
            # 调试时可以看到完整的错误链：LLMError → 原始异常，不会丢失根因信息。
            raise LLMError(ErrorCode.LLM_CALL_FAILED, message) from exc

    def _complete_sync(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """
        在工作线程中执行的同步 LiteLLM 调用（内部私有方法，不对外暴露）。

        功能说明：
            真正发出 HTTP 请求给大模型 API 的地方。
            _ 前缀是 Python 约定的"私有方法"标记，提示外部调用者不要直接使用。

        参数说明：
            messages: 来自 complete() 的消息列表

        返回值：
            LLMResponse，解析自 LiteLLM 返回的原始响应对象

        设计思路：
            把同步逻辑单独抽成方法而不是写在 complete() 里，方便单独进行单元测试——
            测试时可以直接调 _complete_sync()，不需要搭建 async 测试环境。
        """
        """
        🔍 原理讲解：LiteLLM 响应的数据结构（"剥洋葱"过程）

        LiteLLM 返回的是 OpenAI 兼容格式的响应对象，结构大致如下：
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "AI 的回复文字"   ← 我们要的就是这里
                    }
                }
            ],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30
            }
        }

        下面代码的"剥洋葱"路径：
        raw_response → choices 列表 → 取第一个 → message 字典 → content 字符串
        """
        # 💡 学习提示：故意在函数内部 import，而不是放在文件顶部，这叫"延迟导入"。
        # 原因：litellm 是可选依赖，只有真正调用时才加载。
        # 这样即使用户没安装 litellm，整个 athena 模块也能正常导入，只是调用时才报错。
        from litellm import completion

        # 💡 学习提示：model_dump() 把 Pydantic 对象转成普通 dict。
        # litellm.completion() 需要的是字典列表（[{"role": "user", "content": "..."}]），
        # 而不是 LLMMessage 对象，所以这里做一次转换。
        payload = [message.model_dump() for message in messages]
        raw_response = completion(
            model=self.model,
            messages=payload,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        # 💡 学习提示：cast() 是给类型检查器（mypy/pyright）看的"说明注释"，
        # 告诉它"相信我，这个变量的类型是 XXX"。
        # cast() 在运行时什么都不做（零性能开销），纯粹让 IDE 的类型提示正常工作。
        response_map = cast(Mapping[str, object], raw_response)
        choices = cast(Sequence[Mapping[str, object]], response_map.get("choices", []))
        if not choices:
            # 💡 学习提示：防御性编程——正常情况下 choices 不会为空，
            # 但 API 偶尔返回异常结构时，这里提前检查能给出清晰错误信息，而不是 IndexError。
            raise LLMError(ErrorCode.LLM_CALL_FAILED, "LLM returned no choices")
        # 💡 学习提示：choices 里理论上可能有多个候选回复（设置 n>1 时），
        # 这里只取第一个，因为 Athena 默认 n=1。
        first_choice = choices[0]
        message = cast(Mapping[str, object], first_choice.get("message", {}))
        content = str(message.get("content", ""))
        usage = _parse_usage(response_map.get("usage", {}))
        return LLMResponse(content=content, model=self.model, usage=usage)


# ============================================================
# 📌 工厂层：统一的对象创建入口（Factory Pattern）
# ============================================================


class LLMClientFactory:
    """
    LLM 客户端的"工厂"——统一负责创建和验证客户端对象。

    功能说明：
        你不需要直接构造 LiteLLMClient，而是告诉工厂"我要什么配置"，
        工厂帮你检查前置条件、完成初始化、返回可用的客户端。
        就像去餐厅点餐，你不用进厨房自己做，告诉服务员需要什么就行。

    设计思路：
        工厂模式（Factory Pattern）的好处：
        ① 统一入口：所有客户端都从这里创建，不会遗漏检查步骤（如忘记验证 API Key）
        ② 解耦：调用方不需要知道具体类名是 LiteLLMClient，只和工厂打交道
        ③ 易扩展：将来要支持 AzureClient、BedrockClient，只改工厂，调用方代码不变

    使用示例：
        client = LLMClientFactory.create(
            provider="litellm", model="gpt-4o",
            temperature=0.2, max_tokens=1024
        )
        # client 已经过验证，可以直接使用
    """

    @staticmethod
    # 💡 学习提示：@staticmethod 表示这个方法不依赖实例状态（没有 self）。
    # 工厂方法通常设计为静态的，因为"创建对象"这个动作不需要先有一个工厂实例——
    # 如果你必须先创建工厂实例再创建产品，那工厂本身就成了多余的包装。
    def create(
        provider: str, model: str, temperature: float, max_tokens: int
    ) -> LLMClient:
        """
        创建并返回一个经过验证的 LLM 客户端。

        功能说明：
            检查 provider 是否支持、必要的 API Key 是否设置，然后返回可用客户端。

        参数说明：
            provider:    LLM 提供商，目前只支持 "litellm"
            model:       模型标识符（如 "gpt-4o"、"claude-3-5-sonnet-20241022"）
            temperature: 回复随机性（0.0~2.0）
            max_tokens:  最大生成 token 数

        返回值：
            满足 LLMClient 协议的异步客户端对象

        异常：
            LLMError: provider 不支持或 API Key 未设置时抛出

        设计思路：
            "快速失败"（Fail Fast）原则：在对象创建阶段就检查前置条件，
            而不是等到真正调用 complete() 时才发现 Key 没设置。
            这样能在程序启动时就暴露配置错误，而不是在运行中途崩溃。
        """
        if provider != "litellm":
            # ⚡ 优化建议：可以改成 SUPPORTED_PROVIDERS = {"litellm"} 集合，
            # 将来支持新 provider 时只需往集合里加一行，不用修改 if 条件本身，
            # 更符合"开闭原则"（对扩展开放，对修改关闭）。
            raise LLMError(
                ErrorCode.LLM_CALL_FAILED,
                f"Unsupported LLM provider: {provider}",
            )
        # 💡 学习提示：先应用别名映射（如把 OPENAI_API_KEY 复用给 Deepseek），
        # 再检查 Key 是否存在，顺序很重要，不能反过来。
        _apply_provider_env_aliases(model)
        required_env = _required_api_key_env(model)
        if required_env and not os.getenv(required_env):
            # 💡 学习提示：os.getenv() 返回 None 表示环境变量未设置，
            # 这是 Python 中检查环境变量是否配置的标准惯用写法。
            raise LLMError(
                ErrorCode.LLM_CALL_FAILED,
                (
                    f"Missing credentials for model '{model}'. Set {required_env} "
                    "in your PowerShell session or in D:\\mjy-agent\\.env."
                ),
            )
        return LiteLLMClient(
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
        )


# ============================================================
# 📌 工具函数层：私有辅助函数（_ 前缀 = 模块内部使用，外部不应调用）
# ============================================================


def _required_api_key_env(model: str) -> str | None:
    """
    根据模型名前缀判断需要哪个环境变量存放 API Key。

    功能说明：
        不同厂商使用不同的环境变量名存 API Key。
        这个函数通过模型名前缀来推断需要哪个 Key。

    参数说明：
        model: 模型标识符，如 "gpt-4o"、"claude-3-opus"、"deepseek/deepseek-chat"

    返回值：
        需要设置的环境变量名（如 "OPENAI_API_KEY"）；
        未知模型则返回 None（表示跳过检查，允许用户自己管理 Key）

    设计思路：
        用字符串前缀匹配而非完整模型名列表——这样新模型（如 gpt-5）自动支持，
        无需每次手动更新枚举列表。返回 None 而非抛异常，把决策权交给调用方。

    使用示例：
        _required_api_key_env("gpt-4o")              # → "OPENAI_API_KEY"
        _required_api_key_env("claude-3-5-sonnet")   # → "ANTHROPIC_API_KEY"
        _required_api_key_env("deepseek/chat")       # → "DEEPSEEK_API_KEY"
        _required_api_key_env("some-local-llm")      # → None（本地模型不需要 Key）
    """
    # 💡 学习提示：统一转小写避免大小写不一致的问题（"GPT-4o" 和 "gpt-4o" 都能匹配）
    normalized = model.lower()
    if normalized.startswith(("gpt-", "o1", "o3", "o4", "openai/")):
        return "OPENAI_API_KEY"
    if normalized.startswith("claude-"):
        return "ANTHROPIC_API_KEY"
    if normalized.startswith("deepseek/"):
        return "DEEPSEEK_API_KEY"
    return None


def _apply_provider_env_aliases(model: str) -> None:
    """
    为某些 provider 自动映射兼容的 API Key 环境变量。

    功能说明：
        如果用户已设置 OPENAI_API_KEY，访问 Deepseek 时需要 DEEPSEEK_API_KEY。
        若两者 Key 兼容（比如通过中间转发层），这个函数自动复制，省去用户重复配置。

    参数说明：
        model: 模型标识符，用于判断需要哪种映射

    返回值：
        无（直接修改 os.environ 这个全局字典）

    设计思路：
        "约定优于配置"（Convention over Configuration）思想——通过智能推断减少手动配置。
        注意：直接修改 os.environ 是副作用操作（改变全局状态），要谨慎，
        一般只在程序启动时调用一次，不要在循环或并发代码里频繁调用。

    使用示例：
        os.environ["OPENAI_API_KEY"] = "sk-xxx"
        _apply_provider_env_aliases("deepseek/deepseek-chat")
        # 此后 os.environ["DEEPSEEK_API_KEY"] 也会被设置为 "sk-xxx"
    """
    normalized = model.lower()
    if normalized.startswith("deepseek/") and not os.getenv("DEEPSEEK_API_KEY"):
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            # 💡 学习提示：双重判断的设计意图：
            # 第一个条件（not os.getenv("DEEPSEEK_API_KEY")）：已经有 Deepseek Key 就不覆盖
            # 第二个条件（if openai_key）：没有 OpenAI Key 可复用就跳过
            # 这样不会覆盖用户明确设置的 Deepseek Key，也不会在无 Key 可用时报错
            os.environ["DEEPSEEK_API_KEY"] = openai_key


def _sanitize_error_message(message: str) -> str:
    """
    清洗错误信息，防止 API Key 等敏感数据泄露到日志中。

    功能说明：
        API 调用失败时，错误信息里可能包含完整的 API Key（如 "Incorrect key: sk-abc123..."）。
        这个函数把 Key 替换成 "sk-***"，保护敏感信息不被写入日志文件。

    参数说明：
        message: 原始错误信息字符串（可能含有 API Key）

    返回值：
        清洗后的安全错误信息（API Key 已遮蔽）

    设计思路：
        日志脱敏（Log Sanitization）是基本的安全工程要求（对应 OWASP A09 安全日志）。
        日志文件可能被多人查看或上传到监控系统，API Key 一旦泄露就需要立即轮换，代价很高。

    使用示例：
        raw = "Incorrect API key provided: sk-abcdef12345678"
        print(_sanitize_error_message(raw))
        # → "Incorrect API key. Please replace the key in D:\\mjy-agent\\.env."
    """
    # 💡 学习提示：正则表达式 r"sk-[A-Za-z0-9_\-]{8,}" 逐段解读：
    # - sk-              → 字面匹配 "sk-"（OpenAI 风格 Key 的固定前缀）
    # - [A-Za-z0-9_\-]  → 字符集：字母、数字、下划线、横线
    # - {8,}             → 前面的字符集至少出现 8 次（Key 通常很长）
    # 🎯 面试考点：为什么用 {8,} 而不是 +？
    # 答：避免误匹配短字符串（如普通单词里的 "sk-"），要求至少 8 位才认为是 Key。
    redacted = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", message)
    if "Incorrect API key" in redacted:
        # 💡 学习提示：进一步把含敏感关键词的错误替换成用户友好的提示语，
        # 既能防止残留敏感信息，也给用户指明了解决路径（去改 .env 文件）
        return "Incorrect API key. Please replace the key in D:\\mjy-agent\\.env."
    return redacted


def _parse_usage(raw_usage: object) -> dict[str, int]:
    """
    解析 LiteLLM 返回的 token 用量数据，统一为标准字典格式。

    功能说明：
        不同 LLM 提供商返回 token 用量的格式不同——有的是字典，有的是 Pydantic 对象，
        有的是带属性的普通对象。这个函数把所有格式"翻译"成统一的 dict[str, int]。

    参数说明：
        raw_usage: 原始用量数据，类型不确定，可能是：
                   - Mapping（字典）：{"prompt_tokens": 10, ...}
                   - Pydantic 模型：有 model_dump() 方法
                   - 普通对象：有 prompt_tokens 等属性

    返回值：
        标准化的用量字典，如 {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        解析失败时返回空字典 {}（不抛异常，用量统计不是关键路径）

    设计思路：
        用 hasattr() 检测对象能力（鸭子类型），而不是 isinstance() 检查具体类型，
        这样对 LiteLLM 内部实现的变更更健壮——只要对象"长得像"，函数就能处理。

    使用示例：
        _parse_usage({"prompt_tokens": 10, "completion_tokens": 5})
        # → {"prompt_tokens": 10, "completion_tokens": 5}
    """
    """
    🔍 原理讲解：为什么要处理三种不同情况？

    LiteLLM 在不同版本和不同后端下，返回的 usage 类型可能是：
    ① 字典（Mapping）        → {"prompt_tokens": 10, ...}         直接 .items()
    ② Pydantic 模型          → Usage(prompt_tokens=10, ...)        先 model_dump() 转字典
    ③ 普通带属性的对象        → obj.prompt_tokens = 10              逐个 getattr() 读取

    这种"防御性解析"（Defensive Parsing）保证了代码在 LiteLLM 升级后不会轻易崩溃。
    代价是代码稍复杂，但换来了更强的稳定性。
    """
    usage_items: Iterable[tuple[object, object]]
    if isinstance(raw_usage, Mapping):
        usage_items = raw_usage.items()
    elif hasattr(raw_usage, "model_dump"):
        # 💡 学习提示：hasattr(obj, "model_dump") 是判断"是否是 Pydantic 模型"的惯用写法。
        # 比 isinstance(raw_usage, BaseModel) 更宽泛，mock 对象也能通过，单元测试更友好。
        dumped = raw_usage.model_dump()
        usage_items = dumped.items() if isinstance(dumped, Mapping) else ()
    else:
        # 💡 学习提示：这里用生成器表达式而非列表推导式。
        # 生成器是"懒惰求值"的——不会提前计算所有属性，只在后面 dict() 消费时才逐个读取。
        # 对于只有 3 个字段的场景差异不大，但这是良好的编码习惯。
        usage_items = (
            (name, getattr(raw_usage, name))
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if hasattr(raw_usage, name)
        )
    return {
        str(key): int(value)
        for key, value in usage_items
        if isinstance(
            value, int
        )  # 💡 学习提示：过滤掉 None 或其他非整数值，防止 int(None) 抛 TypeError
    }


"""
🤔 思考题（结合这个文件深入思考）：

1. asyncio.to_thread() vs 原生异步：
   如果 LiteLLM 提供了原生的异步接口（如 async_completion()），还需要用 to_thread() 吗？
   你会怎么改 _complete_sync 和 complete 这两个方法？

2. 单一职责挑战：
   LLMClientFactory.create() 同时做了"参数校验"和"对象创建"两件事，
   有人会说这违反了单一职责原则（SRP）——你同意吗？你会怎么拆分？

3. 安全边界：
   _sanitize_error_message() 只处理了 "sk-" 前缀的 Key。
   如果将来用了 Anthropic 的 Key（前缀是 "sk-ant-api03-"）或 Google 的 Key，
   这个函数还够用吗？你能写一个更通用的脱敏版本吗？

4. 测试友好性（依赖注入）：
   现在 LiteLLMClient._complete_sync() 直接调用 litellm.completion，
   单元测试必须真正调用 API（或用 monkeypatch 打桩）。
   如果把 completion 函数作为参数注入进来，代码会怎么变？有什么好处和坏处？

5. （选做）流式输出扩展：
   假设要支持"流式输出"（Streaming，像 ChatGPT 打字机效果），
   LLMResponse 和 LLMClient 协议需要怎么改？
   你会新加一个 stream() 方法，还是改现有的 complete()？
   提示：思考 AsyncGenerator[str, None] 类型。
"""
