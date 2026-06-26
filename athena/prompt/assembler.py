"""
📦 模块名称：提示词上下文组装器（Prompt Context Assembler）
📍 架构位置：提示词层（Prompt Layer）—— 介于 Agent 执行器和 LLM 客户端之间，
           是 ReAct 循环每一步的"翻译官"：
           [ReActAgent] → 【本文件 ContextAssembler】 → [LLMClient]
🎯 核心作用：把分散的 7 份信息（系统提示、记忆、工具列表、草稿等）拼装成
           发给大模型的完整提示词——就像填写一张有 7 个空格的"模板表格"
🔗 依赖关系：
   - 依赖：
     * athena.memory.WorkingMemory   → 提供对话历史（memory 槽位）
     * athena.tools.ToolRegistry     → 提供工具描述列表（tools 槽位）
     * prompts/system/base.md        → 系统级提示（system_prompt 槽位）
     * athena/prompt/templates/react.md → 提示词模板（7 个 {占位符}）
   - 被依赖：athena/agent/executor.py（ReActAgent 每一步都调用 build_prompt）
💡 设计思路：
   "模板方法模式"（Template Pattern）的变体——把提示词分成"稳定部分"和"动态部分"：
   ① 稳定部分（对象初始化时确定）：system_prompt、static_context、output_contract
      → 几乎每步都一样，只加载一次逻辑上就够了
   ② 动态部分（每次调用 build_prompt 时传入）：memory、tools、scratchpad、query
      → 每步都在变化，必须实时获取

   模板文件（react.md）用 Python 内置的 str.format() 做占位符替换，
   零依赖、简单直接，优于 Jinja2 等重量级模板引擎（MVP 阶段够用）。
📚 学习重点：
   1. 为什么要把提示词拼接逻辑单独抽成一个类？（关注点分离）
   2. str.format() 占位符替换的工作原理
   3. _read_optional() 的"柔性加载"设计：文件不存在不报错
   4. output_contract 字段的作用——告诉 LLM 输出什么格式
   5. 7 个槽位的分工：哪些稳定、哪些动态
"""

from __future__ import annotations  # 💡 学习提示：全项目统一风格，支持类型注解前向引用

import logging
from pathlib import (  # 💡 学习提示：用 Path 而非字符串处理路径，跨平台兼容（Windows/Linux 斜杠问题）
    Path,
)

from pydantic import BaseModel, Field

from athena.exceptions import ErrorCode, PromptError
from athena.memory import WorkingMemory
from athena.tools import ToolRegistry

logger = logging.getLogger(
    __name__
)  # 💡 学习提示：模块级 logger，日志显示 "athena.prompt.assembler"


class ContextAssembler(BaseModel):
    """
    ReAct 循环每一步的提示词组装器。

    功能说明：
        管理 7 个提示词槽位，在 build_prompt() 时把动态内容填入模板，
        生成完整的提示词字符串，交给 LLM 进行下一步推理。

        可以把它想象成一张"填空题答卷"：
        - react.md 是答卷（有 7 个括号要填）
        - ContextAssembler 负责找到正确答案填进去

    字段说明（4 个配置项）：

        system_prompt_path:
            系统提示词文件的路径，填入模板的第一个槽位。
            内容是给 AI 的"角色设定"（如"你是 Athena Agent，一个企业级助手..."）。
            文件不存在时自动降级为空字符串（不报错）。
            默认值：prompts/system/base.md

        template_path:
            提示词模板文件的路径，包含 7 个 {占位符}。
            这是最核心的配置——模板决定了"每次给 LLM 的信息结构"。
            文件不存在时降级为空字符串（但会导致 format 找不到占位符而报错，见 KeyError 处理）。
            默认值：athena/prompt/templates/react.md

        static_context:
            静态补充上下文，用于注入领域知识或项目特有说明。
            示例："本系统运行在 Windows 环境，所有路径使用反斜杠。"
            大多数情况下留空。

        output_contract:
            输出格式约定——告诉 LLM 必须按什么格式回复。
            这是保证 _parse_decision() 能正确解析 LLM 响应的关键！
            默认值要求 LLM 返回带固定字段的 JSON。

    设计思路：
        继承 Pydantic BaseModel 的好处：
        ① 字段有默认值，最简单的用法是 ContextAssembler()，零配置即可运行
        ② 路径类型用 Path 而不是 str，Pydantic 会自动转换字符串为 Path 对象
        ③ 配置可以来自 config.yaml，Pydantic 支持从字典直接构造

    # 🎯 面试考点：为什么把 output_contract 放在类字段里，而不是硬编码在 build_prompt() 里？
    # 答：① 可配置性——将来改变输出格式（如从 JSON 改成 YAML）只需修改配置，不改代码
    #     ② 可测试性——测试时可以传入自定义 output_contract，验证不同格式约定的行为
    #     ③ 单一来源——所有的"格式要求"集中在一个地方，不散落在代码各处

    使用示例：
        # 零配置，使用所有默认值
        assembler = ContextAssembler()

        # 自定义系统提示词路径和补充上下文
        assembler = ContextAssembler(
            system_prompt_path=Path("prompts/system/custom.md"),
            static_context="本系统服务于金融行业，请遵守合规要求。"
        )

        prompt = assembler.build_prompt(
            query="计算 100+200",
            memory=working_memory,
            tools=tool_registry,
            scratchpad="",
        )
    """

    system_prompt_path: Path = Path("prompts/system/base.md")
    template_path: Path = Path("athena/prompt/templates/react.md")
    static_context: str = ""
    # 💡 学习提示：用 Field(default=...) 而不是直接赋值，是因为 default 值较长，
    # Field 可以添加 description 等元信息，让配置更自文档化
    output_contract: str = Field(
        default=(
            "Return JSON only with keys: thought, action, action_input, final_answer. "
            "Use action=null when final_answer is ready."
        )
    )

    def build_prompt(
        self,
        query: str,
        memory: WorkingMemory,
        tools: ToolRegistry,
        scratchpad: str,
    ) -> str:
        """
        将 7 份信息组装成一个完整的 LLM 提示词字符串。

        功能说明：
            这是 ContextAssembler 的唯一对外接口。
            每次 ReAct 循环的一步，executor.py 都会调用这个方法，
            拿到最新的完整提示词，再发给 LLM。

        参数说明：
            query:      用户的原始问题（整个任务期间不变）
                        示例："帮我计算 1234 * 5678"

            memory:     WorkingMemory 对象，调用 .render() 后得到历史消息的文字
                        示例渲染结果："user: 你好\nassistant: 你好，有什么可以帮你？"

            tools:      ToolRegistry 对象，调用 .describe_tools() 后得到工具描述
                        示例渲染结果："- calculator(expression: str): Evaluate a math expression"

            scratchpad: 本次任务到目前为止的思考历史（随循环步骤累积）
                        第 1 步时为空字符串，第 2 步后会有 Step1 的 Thought/Action/Observation

        返回值：
            完整的提示词字符串，可直接传给 LLMClient.complete()

        异常：
            PromptError: 文件读取失败（OSError）或模板占位符不匹配（KeyError）时抛出

        设计思路：
            "流水线组装"——先各自准备材料（system_prompt、tool_descriptions），
            再用 str.format() 一次性填入模板，最终得到完整提示词。
            中间的 try/except 统一处理两种可能的错误。
        """
        """
        🔍 原理讲解：7 个槽位是怎么拼在一起的？

        react.md 模板长这样（每个 {xxx} 是一个占位符槽位）：

        {system_prompt}          ← 角色设定（"你是 Athena Agent..."）

        Static Context:
        {static_context}         ← 静态补充说明（通常为空）

        Working Memory:
        {memory}                 ← 对话历史（"user: ...\nassistant: ..."）

        Available Tools:
        {tools}                  ← 工具列表（"- calculator(expr: str): ..."）

        Scratchpad:
        {scratchpad}             ← 本轮任务的历史步骤（逐步累积）

        User Query:
        {query}                  ← 用户的原始问题

        Output Contract:
        {output_contract}        ← 输出格式要求（"Return JSON only..."）

        str.format() 把 7 个变量一一填入对应的 {xxx} 位置，就像"填空题"。

        举个第 2 步时的 scratchpad 示例：
        输入 scratchpad = "Step 1\nThought: 需要计算\nAction: calculator\nObservation: 7006652"
        → 这段文字填入 {scratchpad} 位置，让 AI 知道第 1 步做了什么，
          从而在第 2 步做出"已经有结果了，可以给出最终答案"的判断。
        """
        try:
            # 💡 学习提示：_read_optional() 读取文件，文件不存在时返回空字符串而不报错
            # 这样即使 prompts/system/base.md 被删除，Agent 也能降级运行
            system_prompt = self._read_optional(self.system_prompt_path)
            template = self._read_optional(self.template_path)
            # 💡 学习提示：.describe_tools() 把 ToolRegistry 里所有工具转成可读文字，
            # 类似："- calculator(expression: str): Evaluate a math expression\n- echo(text: str): ..."
            tool_descriptions = tools.describe_tools()
            # 💡 学习提示：str.format() 是 Python 内置的模板填充机制，
            # 它会把字符串里的 {key} 替换为对应的关键字参数值。
            # 如果模板里有 {unknown_key}（字典里没有），会抛 KeyError，被下面 except 捕获。
            return template.format(
                system_prompt=system_prompt,
                static_context=self.static_context,
                memory=memory.render(),  # 💡 学习提示：.render() 把消息列表转成 "role: content" 的多行文字
                tools=tool_descriptions,
                scratchpad=scratchpad,
                query=query,
                output_contract=self.output_contract,
            )
        except (OSError, KeyError) as exc:
            # 💡 学习提示：同时捕获两种异常：
            # - OSError：文件读取失败（磁盘错误、权限问题等）
            # - KeyError：模板里有 {xxx} 但 format() 的参数里没有对应的 xxx
            # 统一转换为 PromptError，让上层（executor.py）只需处理 Athena 自己的异常体系
            logger.exception("Prompt assembly failed")
            raise PromptError(ErrorCode.PROMPT_BUILD_FAILED, str(exc)) from exc

    def _read_optional(self, path: Path) -> str:
        """
        柔性文件读取——文件存在则读取，不存在则静默返回空字符串。

        功能说明：
            对 system_prompt 和 template 两个文件都使用此方法读取，
            不存在时不报错，而是返回 ""，让调用方决定如何处理空值。

        参数说明：
            path: 要读取的文件路径（Path 对象）

        返回值：
            文件内容字符串（已去除首尾空白）；文件不存在时返回空字符串 ""

        设计思路：
            "宽容加载"（Graceful Loading）——区别于"文件不存在就报错"的严格模式。
            好处：
            ① 新项目初始化时即使还没有 system_prompt，Agent 也能先跑起来
            ② 单元测试不需要准备文件就能测试 build_prompt 的核心逻辑
            ③ 如果 template 文件也不存在，会返回空字符串，
               之后 str.format() 在空字符串上调用，不会填入任何内容，
               最终 build_prompt 返回空字符串（不报错但功能失效，是一个取舍）

        # ⚡ 优化建议：目前 template 不存在时静默返回 ""，导致 format() 输出空字符串，
        # Agent 发给 LLM 的是空提示词，会产生随机输出。
        # 可以考虑：对 template_path 专门检查，文件不存在时直接抛 PromptError，
        # 快速失败比悄悄出错更容易排查问题。

        使用示例：
            content = self._read_optional(Path("prompts/system/base.md"))
            # 如果文件存在：content = "You are Athena Agent..."
            # 如果文件不存在：content = ""
        """
        if not path.exists():
            # 💡 学习提示：path.exists() 是 pathlib.Path 提供的方法，跨平台检查文件是否存在，
            # 比 os.path.exists() 更面向对象，与 pathlib 的整体 API 风格一致
            return ""
        # 💡 学习提示：.strip() 去除文件首尾的空行/空白，
        # 避免文件末尾多一个换行符导致拼接时出现多余空行
        return path.read_text(encoding="utf-8").strip()
        # 💡 学习提示：显式指定 encoding="utf-8" 是好习惯，
        # 不指定时默认用系统编码（Windows 上是 GBK），如果文件含中文可能乱码


"""
🤔 思考题（结合 react.md 模板和 assembler.py 一起思考）：

1. 模板引擎的选择：
   现在用 str.format() 做占位符替换，简单但有局限——
   比如无法在模板里写条件判断（如"如果没有 scratchpad，就不显示 Scratchpad 部分"）。
   如果用 Jinja2 模板引擎，能实现什么功能？什么时候值得引入这个依赖？

2. 提示词长度控制：
   随着 memory 越来越多、scratchpad 越来越长，组装后的提示词会超过 LLM 的上下文限制。
   当前代码有任何字数/Token 限制吗？你会在哪里加长度控制？
   提示：memory 层的 WorkingMemory 已有 max_tokens 机制，scratchpad 呢？

3. output_contract 的脆弱性：
   output_contract 要求 LLM 返回 JSON，但 LLM 并不总是遵守。
   _parse_decision 做了降级处理，但有没有更从根本上解决这个问题的方法？
   提示：了解一下 OpenAI 的 "Structured Outputs" / "response_format" 参数。

4. 多语言支持：
   系统提示词是英文的，但用户可能用中文提问。
   你觉得 system_prompt 应该改成中文吗？还是让 LLM 自动适配语言？
   如果要支持中英文切换，你会怎么设计 ContextAssembler？

5. （选做）提示词版本管理：
   随着项目演进，react.md 模板会频繁修改（增减槽位、调整措辞）。
   如果同时有多个版本的模板在运行（灰度发布），你会怎么改造 ContextAssembler 来支持？
"""
