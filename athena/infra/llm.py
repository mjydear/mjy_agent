"""LLM gateway abstractions and LiteLLM implementation."""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from athena.exceptions import ErrorCode, LLMError

logger = logging.getLogger(__name__)


class LLMMessage(BaseModel):
    """A chat message sent to the LLM gateway."""

    role: str
    content: str


class LLMResponse(BaseModel):
    """Normalized LLM response used by Athena internals."""

    content: str
    model: str
    usage: Mapping[str, int] = Field(default_factory=dict)


class LLMClient(Protocol):
    """Protocol for asynchronous LLM clients."""

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """Return a completion for the provided chat messages."""


class LiteLLMClient(BaseModel):
    """LiteLLM-backed client with a provider-neutral Athena interface."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    model: str
    temperature: float = 0.2
    max_tokens: PositiveInt = 1024

    async def complete(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """Call LiteLLM without blocking the event loop.

        Args:
            messages: Ordered chat messages.

        Returns:
            Normalized LLM response.

        Raises:
            LLMError: If LiteLLM cannot be imported or the call fails.
        """
        try:
            return await asyncio.to_thread(self._complete_sync, messages)
        except Exception as exc:
            message = _sanitize_error_message(str(exc))
            logger.error("LLM completion failed: %s", message)
            raise LLMError(ErrorCode.LLM_CALL_FAILED, message) from exc

    def _complete_sync(self, messages: Sequence[LLMMessage]) -> LLMResponse:
        """Run the blocking LiteLLM call in a worker thread."""
        from litellm import completion

        payload = [message.model_dump() for message in messages]
        raw_response = completion(
            model=self.model,
            messages=payload,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )
        response_map = cast(Mapping[str, object], raw_response)
        choices = cast(Sequence[Mapping[str, object]], response_map.get("choices", []))
        if not choices:
            raise LLMError(ErrorCode.LLM_CALL_FAILED, "LLM returned no choices")
        first_choice = choices[0]
        message = cast(Mapping[str, object], first_choice.get("message", {}))
        content = str(message.get("content", ""))
        usage = _parse_usage(response_map.get("usage", {}))
        return LLMResponse(content=content, model=self.model, usage=usage)


class LLMClientFactory:
    """Factory for creating provider-neutral LLM clients."""

    @staticmethod
    def create(provider: str, model: str, temperature: float, max_tokens: int) -> LLMClient:
        """Create an LLM client.

        Args:
            provider: Provider name. Only ``litellm`` is supported in MVP.
            model: Model identifier accepted by LiteLLM.
            temperature: Sampling temperature.
            max_tokens: Maximum output tokens.

        Returns:
            An asynchronous LLM client.

        Raises:
            LLMError: If provider is unsupported.
        """
        if provider != "litellm":
            raise LLMError(
                ErrorCode.LLM_CALL_FAILED,
                f"Unsupported LLM provider: {provider}",
            )
        _apply_provider_env_aliases(model)
        required_env = _required_api_key_env(model)
        if required_env and not os.getenv(required_env):
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


def _required_api_key_env(model: str) -> str | None:
    """Return the expected API-key environment variable for known models."""
    normalized = model.lower()
    if normalized.startswith(("gpt-", "o1", "o3", "o4", "openai/")):
        return "OPENAI_API_KEY"
    if normalized.startswith("claude-"):
        return "ANTHROPIC_API_KEY"
    if normalized.startswith("deepseek/"):
        return "DEEPSEEK_API_KEY"
    return None


def _apply_provider_env_aliases(model: str) -> None:
    """Map compatible API-key environment variables for known providers."""
    normalized = model.lower()
    if normalized.startswith("deepseek/") and not os.getenv("DEEPSEEK_API_KEY"):
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            os.environ["DEEPSEEK_API_KEY"] = openai_key


def _sanitize_error_message(message: str) -> str:
    """Redact credential-like strings from provider error messages."""
    redacted = re.sub(r"sk-[A-Za-z0-9_\-]{8,}", "sk-***", message)
    if "Incorrect API key" in redacted:
        return "Incorrect API key. Please replace the key in D:\\mjy-agent\\.env."
    return redacted


def _parse_usage(raw_usage: object) -> dict[str, int]:
    """Normalize LiteLLM usage data across providers."""
    if isinstance(raw_usage, Mapping):
        usage_items = raw_usage.items()
    elif hasattr(raw_usage, "model_dump"):
        dumped = raw_usage.model_dump()
        usage_items = dumped.items() if isinstance(dumped, Mapping) else ()
    else:
        usage_items = (
            (name, getattr(raw_usage, name))
            for name in ("prompt_tokens", "completion_tokens", "total_tokens")
            if hasattr(raw_usage, name)
        )
    return {
        str(key): int(value)
        for key, value in usage_items
        if isinstance(value, int)
    }
