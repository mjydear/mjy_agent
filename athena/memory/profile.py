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
        for name, values in (("preferences", self.preferences), ("coding_style", self.coding_style), ("tech_stack", self.tech_stack)):
            if values:
                facts = ", ".join(f"{key}={value}" for key, value in sorted(values.items()))
                sections.append(f"{name}: {facts}")
        return "\n".join(sections)

    def _target(self, category: str) -> dict[str, str]:
        if category == "preferences":
            return self.preferences
        if category == "coding_style":
            return self.coding_style
        if category == "tech_stack":
            return self.tech_stack
        raise ValueError("profile category must be preferences, coding_style or tech_stack")


class ProfileCurator:
    """
    用户画像复盘器骨架。

    功能说明：
        当前版本调用轻量规则抽取；未来可以把 review() 替换成 LLM 总结，
        但 UserProfile 的更新接口保持不变，符合开闭原则。
    """

    def __init__(self, profile: UserProfile) -> None:
        self.profile = profile

    async def review(self, conversation_text: str) -> bool:
        """Review conversation text and update the profile when useful."""
        return self.profile.learn_from_text(conversation_text)