"""用户画像 LLM 抽取 + 规则降级测试。"""

from __future__ import annotations

import pytest

from athena.infra.llm import LLMResponse
from athena.memory.profile import ProfileCurator, UserProfile


class _StubLLM:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def complete(self, messages):  # noqa: ANN001
        self.calls += 1
        return LLMResponse(content=self._content, model="stub")


class _BrokenLLM:
    async def complete(self, messages):  # noqa: ANN001
        raise RuntimeError("llm down")


@pytest.mark.asyncio
async def test_curator_llm_extraction() -> None:
    llm = _StubLLM(
        '{"tech_stack.language": "Rust", "preferences.answer_style": "concise"}'
    )
    profile = UserProfile()
    curator = ProfileCurator(profile, llm_client=llm)
    updated = await curator.review("我主要用 Rust，请简洁点")
    assert updated is True
    assert llm.calls == 1
    assert profile.tech_stack["language"] == "Rust"
    assert profile.preferences["answer_style"] == "concise"


@pytest.mark.asyncio
async def test_curator_falls_back_to_rules_on_llm_error() -> None:
    profile = UserProfile()
    curator = ProfileCurator(profile, llm_client=_BrokenLLM())
    updated = await curator.review("I love pytest and python")
    assert updated is True
    assert profile.tech_stack["test"] == "pytest"


@pytest.mark.asyncio
async def test_curator_without_llm_uses_rules() -> None:
    profile = UserProfile()
    curator = ProfileCurator(profile)
    updated = await curator.review("用 TypeScript 写前端")
    assert updated is True
    assert profile.tech_stack["language"] == "TypeScript"


@pytest.mark.asyncio
async def test_curator_empty_text_returns_false() -> None:
    profile = UserProfile()
    curator = ProfileCurator(profile, llm_client=_StubLLM("{}"))
    assert await curator.review("   ") is False
