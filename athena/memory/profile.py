"""
📦 模块名称：用户画像（User Profile）
📍 架构位置：记忆层（Memory Layer）—— 位于对话历史和长期记忆之间：
              [Conversation / Trace] → 【UserProfile】 → [Prompt Context]
🎯 核心作用：自动学习用户偏好、编码风格和技术栈选择，让 Agent 的回答逐步贴合个人习惯。
🔗 依赖关系：
    - 依赖：标准库 dataclass/time
    - 被依赖：Prompt 组装器、后台 Curator、长期记忆复盘流程
💡 设计思路：
    用户画像不是每轮对话都更新，而是带节流的增量更新：
    ① update_interval_seconds 控制更新频率，避免频繁总结浪费 Token
    ② signals 使用 category.field 的扁平 key，便于从 LLM 抽取结果直接合并
    ③ render() 输出简洁事实，方便直接注入提示词

📚 学习重点：
    1. 为什么用户画像需要节流：画像更新是慢路径，不应该拖慢主交互
    2. 为什么分 preferences/coding_style/tech_stack 三类：面试时可讲领域建模清晰
    3. 为什么 learn_from_text 先做轻量规则：MVP 可运行，未来可替换成 LLM 抽取器
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class UserProfile:
    """
    用户画像数据模型。

    字段说明：
        preferences:  用户回答风格、交互习惯等偏好
        coding_style: 命名、注释、测试风格等编码习惯
        tech_stack:   常用语言、框架、测试工具等技术栈选择

    设计思路：
        使用普通 dataclass 而不是 Pydantic，是因为这里主要是运行时可变状态，
        不需要复杂序列化；后续如果要落盘，可以再加 repository 层。
    """

    preferences: dict[str, str] = field(default_factory=dict)
    coding_style: dict[str, str] = field(default_factory=dict)
    tech_stack: dict[str, str] = field(default_factory=dict)
    update_interval_seconds: float = 300.0
    last_updated_at: float = 0.0

    def update(self, signals: dict[str, str], force: bool = False) -> bool:
        """
        增量更新画像事实。

        参数说明：
            signals: 形如 {"tech_stack.language": "Python"} 的扁平信号字典
            force:   是否绕过节流限制，适合后台复盘或测试

        返回值：
            True 表示本次发生了更新；False 表示因为节流或无有效信号而跳过。
        """
        if not isinstance(signals, dict):
            raise ValueError("signals must be a dictionary")
        now = time.time()
        if not force and now - self.last_updated_at < self.update_interval_seconds:
            return False
        for key, value in signals.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("profile signals must be string key/value pairs")
            category, _, field_name = key.partition(".")
            if not field_name:
                category, field_name = "preferences", category
            self._target(category)[field_name] = value
        self.last_updated_at = now
        return True

    def learn_from_text(self, text: str, force: bool = False) -> bool:
        """Extract a small set of profile signals from user text."""
        if not isinstance(text, str):
            raise ValueError("text must be a string")
        lowered = text.lower()
        signals: dict[str, str] = {}
        if "pytest" in lowered:
            signals["tech_stack.test"] = "pytest"
        if "typescript" in lowered:
            signals["tech_stack.language"] = "TypeScript"
        if "python" in lowered:
            signals["tech_stack.language"] = "Python"
        if "简洁" in text or "concise" in lowered:
            signals["preferences.answer_style"] = "concise"
        if "详细" in text or "explain" in lowered:
            signals["preferences.answer_style"] = "detailed"
        if not signals:
            return False
        return self.update(signals, force=force)

    def render(self) -> str:
        """Render profile facts for prompt context."""
        sections = []
        for name, values in (
            ("preferences", self.preferences),
            ("coding_style", self.coding_style),
            ("tech_stack", self.tech_stack),
        ):
            if values:
                facts = ", ".join(
                    f"{key}={value}" for key, value in sorted(values.items())
                )
                sections.append(f"{name}: {facts}")
        return "\n".join(sections)

    def _target(self, category: str) -> dict[str, str]:
        if category == "preferences":
            return self.preferences
        if category == "coding_style":
            return self.coding_style
        if category == "tech_stack":
            return self.tech_stack
        raise ValueError(
            "profile category must be preferences, coding_style or tech_stack"
        )


class ProfileCurator:
    """
    用户画像复盘器：LLM 抽取偏好信号，缺失时降级规则。

    功能说明：
        review() 优先用 LLM 从对话文本抽取扁平画像信号并更新画像；
        无 LLM 客户端或抽取失败时降级到 learn_from_text 规则抽取。
    参数说明：
        profile：待更新的用户画像。
        llm_client：可选 LLM 客户端，注入后走真实语义抽取。
    设计思路：企业级“真实实现 + 自动降级”，保证任何环境下画像都能持续演进。
    """

    def __init__(
        self, profile: UserProfile, llm_client: "object | None" = None
    ) -> None:
        self.profile = profile
        self.llm_client = llm_client

    async def review(self, conversation_text: str) -> bool:
        """Review conversation text and update the profile when useful."""
        if not isinstance(conversation_text, str) or not conversation_text.strip():
            return False
        if self.llm_client is not None:
            signals = await self._llm_extract(conversation_text)
            if signals:
                return self.profile.update(signals, force=True)
        return self.profile.learn_from_text(conversation_text)

    async def _llm_extract(self, text: str) -> dict[str, str]:
        """用 LLM 抽取画像信号，失败返回空 dict 交由规则兜底。"""
        import json
        import logging

        logger = logging.getLogger(__name__)
        try:
            from athena.infra.llm import LLMMessage

            prompt = (
                "从下面的用户对话中抽取稳定的用户画像信号，只输出 JSON 对象，"
                "key 用 'preferences.xxx' / 'coding_style.xxx' / 'tech_stack.xxx' 形式，"
                "value 为字符串；没有可抽取信息时输出 {}。\n"
                f"对话：{text.strip()}"
            )
            response = await self.llm_client.complete(  # type: ignore[union-attr]
                [LLMMessage(role="user", content=prompt)]
            )
            raw = response.content or ""
            start = raw.find("{")
            end = raw.rfind("}")
            if start == -1 or end == -1 or end <= start:
                return {}
            payload = json.loads(raw[start : end + 1])
            return {
                str(key): str(value)
                for key, value in payload.items()
                if isinstance(key, str) and value is not None
            }
        except Exception as exc:
            logger.warning("LLM profile extraction failed, using rules: %s", exc)
            return {}
