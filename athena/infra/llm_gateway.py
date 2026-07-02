"""
LLM Gateway - Enterprise-grade LLM access layer with connection pooling,
multi-provider load balancing, automatic failover, and Structured Outputs.

Replaces the MVP asyncio.to_thread + sync litellm approach with:
- httpx.AsyncClient connection pooling
- Multi-provider weighted load balancing
- Circuit breaker pattern for automatic failover
- response_format support for Structured Outputs
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, PositiveInt

from athena.exceptions import ErrorCode, LLMError
from athena.infra.llm import (
    LLMClient,
    LLMMessage,
    LLMResponse,
    _parse_usage,
    _sanitize_error_message,
)

logger = logging.getLogger(__name__)


# ============================================================
# Circuit Breaker
# ============================================================


class CircuitState(Enum):
    """Circuit breaker states following the standard CLOSED→OPEN→HALF_OPEN pattern."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Per-provider circuit breaker.

    State transitions:
        CLOSED ──连续失败 N 次──> OPEN ──等待 recovery_timeout 秒──> HALF_OPEN
          ^                                                             │
          └──────────────── 成功 ──────────────────────────────────────┘
          └── 失败 ──> OPEN
    """

    failure_threshold: int = 5
    recovery_timeout: float = 30.0

    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    last_success_time: float = 0.0

    def record_success(self) -> None:
        """Reset to CLOSED on success."""
        self.state = CircuitState.CLOSED
        self.failure_count = 0
        self.last_success_time = time.monotonic()

    def record_failure(self) -> None:
        """Increment failure count; open circuit if threshold reached."""
        self.failure_count += 1
        self.last_failure_time = time.monotonic()
        if self.failure_count >= self.failure_threshold:
            self.state = CircuitState.OPEN
            logger.warning(
                "Circuit breaker OPEN after %d consecutive failures",
                self.failure_count,
            )

    def allow_request(self) -> bool:
        """Check if a request should be allowed through."""
        if self.state == CircuitState.CLOSED:
            return True
        if self.state == CircuitState.OPEN:
            elapsed = time.monotonic() - self.last_failure_time
            if elapsed >= self.recovery_timeout:
                self.state = CircuitState.HALF_OPEN
                logger.info("Circuit breaker entering HALF_OPEN for probe")
                return True
            return False
        # HALF_OPEN: allow one probe request
        return True


# ============================================================
# Provider Config & State
# ============================================================


class LLMProviderConfig(BaseModel):
    """Configuration for a single LLM provider endpoint."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    name: str = Field(description="Human-readable provider identifier, e.g. 'openai-primary'")
    provider: str = Field(
        description="Provider type: openai, azure, deepseek, anthropic, etc."
    )
    model: str = Field(description="Model name, e.g. 'gpt-4o', 'deepseek-chat'")
    api_key_env: str = Field(
        description="Environment variable name for the API key, e.g. 'OPENAI_API_KEY'"
    )
    api_base: str | None = Field(
        default=None,
        description="Custom API base URL for proxies or Azure endpoints",
    )
    weight: float = Field(
        default=1.0, ge=0.0, description="Load balancing weight (higher = more traffic)"
    )
    timeout: float = Field(
        default=60.0, ge=1.0, description="Request timeout in seconds"
    )
    max_retries: int = Field(
        default=3, ge=0, description="Max retry attempts per provider"
    )


@dataclass
class ProviderState:
    """Runtime state for a single provider, including circuit breaker and latency stats."""

    config: LLMProviderConfig
    circuit_breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    total_requests: int = 0
    total_failures: int = 0
    total_latency: float = 0.0
    _round_robin_counter: int = 0

    @property
    def avg_latency(self) -> float:
        """Average latency in seconds."""
        if self.total_requests == 0:
            return 0.0
        return self.total_latency / self.total_requests

    @property
    def is_healthy(self) -> bool:
        """Whether the circuit breaker allows requests through."""
        return self.circuit_breaker.allow_request()


# ============================================================
# Provider Manager
# ============================================================


class ProviderManager:
    """Manages multiple LLM providers with weighted/RR load balancing and failover."""

    def __init__(self, providers: list[LLMProviderConfig], strategy: str = "weighted"):
        if not providers:
            raise LLMError(
                ErrorCode.LLM_CALL_FAILED,
                "At least one LLM provider is required",
            )
        self._providers: dict[str, ProviderState] = {
            p.name: ProviderState(config=p) for p in providers
        }
        self._strategy = strategy
        self._lock = asyncio.Lock()

    async def select_provider(self) -> ProviderState:
        """Select a healthy provider using the configured load balancing strategy.

        If all providers are unhealthy, returns the one with the oldest failure
        as a last-resort attempt.
        """
        async with self._lock:
            healthy = [p for p in self._providers.values() if p.is_healthy]
            if not healthy:
                oldest = min(
                    self._providers.values(),
                    key=lambda p: p.circuit_breaker.last_failure_time,
                )
                logger.warning(
                    "All providers unhealthy, attempting %s as last resort",
                    oldest.config.name,
                )
                return oldest

            if self._strategy == "weighted":
                return self._select_weighted(healthy)
            return self._select_round_robin(healthy)

    def _select_weighted(self, healthy: list[ProviderState]) -> ProviderState:
        """Weighted random selection."""
        total_weight = sum(p.config.weight for p in healthy)
        if total_weight <= 0:
            return healthy[0]
        r = random.uniform(0, total_weight)
        cumulative = 0.0
        for p in healthy:
            cumulative += p.config.weight
            if r <= cumulative:
                return p
        return healthy[-1]

    def _select_round_robin(self, healthy: list[ProviderState]) -> ProviderState:
        """Simple round-robin selection."""
        selected = min(healthy, key=lambda p: p._round_robin_counter)
        selected._round_robin_counter += 1
        return selected

    async def record_result(
        self, name: str, success: bool, latency: float
    ) -> None:
        """Record the result of a provider call, updating circuit breaker state."""
        async with self._lock:
            state = self._providers.get(name)
            if state is None:
                return
            state.total_requests += 1
            state.total_latency += latency
            if success:
                state.circuit_breaker.record_success()
            else:
                state.total_failures += 1
                state.circuit_breaker.record_failure()

    def get_stats(self) -> dict[str, dict[str, object]]:
        """Return health statistics for all providers."""
        return {
            name: {
                "state": state.circuit_breaker.state.value,
                "total_requests": state.total_requests,
                "total_failures": state.total_failures,
                "avg_latency_ms": round(state.avg_latency * 1000, 2),
                "healthy": state.is_healthy,
            }
            for name, state in self._providers.items()
        }


# ============================================================
# LLM Gateway Config
# ============================================================


class LLMGatewayConfig(BaseModel):
    """Configuration for the LLM Gateway."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    providers: list[LLMProviderConfig] = Field(
        default_factory=list,
        description="List of LLM provider configurations",
    )
    default_temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    default_max_tokens: PositiveInt = 1024
    load_balancing: Literal["round_robin", "weighted"] = Field(
        default="weighted",
        description="Load balancing strategy",
    )
    circuit_breaker_failures: int = Field(
        default=5, ge=1, description="Consecutive failures before circuit opens"
    )
    circuit_breaker_recovery: float = Field(
        default=30.0, ge=1.0, description="Seconds before circuit half-opens"
    )
    connection_pool_size: int = Field(
        default=100, ge=1, description="Max total connections in pool"
    )
    connection_pool_per_host: int = Field(
        default=20, ge=1, description="Max connections per host"
    )


# ============================================================
# LLM Gateway
# ============================================================


class LLMGateway:
    """Enterprise LLM Gateway.

    Features:
    - Shared httpx.AsyncClient with connection pooling across all providers
    - Multi-provider load balancing (weighted / round-robin)
    - Automatic failover when a provider fails
    - Circuit breaker pattern to avoid hammering dead providers
    - Structured Outputs via response_format (OpenAI json_schema)
    - Implements LLMClient Protocol for drop-in compatibility

    Usage:
        config = LLMGatewayConfig(
            providers=[
                LLMProviderConfig(
                    name="openai-primary",
                    provider="openai",
                    model="gpt-4o",
                    api_key_env="OPENAI_API_KEY",
                    weight=3,
                ),
                LLMProviderConfig(
                    name="deepseek-backup",
                    provider="deepseek",
                    model="deepseek-chat",
                    api_key_env="DEEPSEEK_API_KEY",
                    weight=1,
                ),
            ],
        )
        gateway = LLMGateway(config)
        response = await gateway.complete([LLMMessage(role="user", content="Hello")])
        print(response.content)
    """

    def __init__(self, config: LLMGatewayConfig):
        self._config = config
        self._http_client = httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=config.connection_pool_size,
                max_keepalive_connections=config.connection_pool_per_host,
            ),
            timeout=httpx.Timeout(60.0),
        )
        self._provider_manager = ProviderManager(
            providers=config.providers,
            strategy=config.load_balancing,
        )
        # Apply circuit breaker settings from config
        for state in self._provider_manager._providers.values():
            state.circuit_breaker.failure_threshold = config.circuit_breaker_failures
            state.circuit_breaker.recovery_timeout = config.circuit_breaker_recovery

    async def complete(
        self,
        messages: Sequence[LLMMessage],
        temperature: float | None = None,
        max_tokens: int | None = None,
        response_format: dict[str, object] | None = None,
    ) -> LLMResponse:
        """Send a completion request with automatic failover across providers.

        Args:
            messages: Chat messages to send.
            temperature: Override default temperature (0.0-2.0).
            max_tokens: Override default max tokens.
            response_format: OpenAI-style response_format dict for Structured Outputs.

        Returns:
            LLMResponse with content, model name, and token usage.

        Raises:
            LLMError: When all providers have failed.
        """
        tried_providers: list[str] = []
        last_error: Exception | None = None
        provider_count = len(self._provider_manager._providers)

        for _ in range(provider_count):
            state = await self._provider_manager.select_provider()
            if state.config.name in tried_providers:
                continue
            tried_providers.append(state.config.name)

            try:
                result = await self._call_provider(
                    state, messages, temperature, max_tokens, response_format
                )
                await self._provider_manager.record_result(
                    state.config.name, success=True, latency=result._latency
                )
                return result
            except Exception as exc:
                last_error = exc
                logger.warning(
                    "Provider '%s' failed: %s",
                    state.config.name,
                    _sanitize_error_message(str(exc)),
                )
                await self._provider_manager.record_result(
                    state.config.name, success=False, latency=0.0
                )

        raise LLMError(
            ErrorCode.LLM_ALL_PROVIDERS_FAILED,
            f"All providers failed. Tried: {tried_providers}. "
            f"Last error: {last_error}",
        )

    async def complete_structured(
        self,
        messages: Sequence[LLMMessage],
        json_schema: dict[str, object],
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Send a completion with Structured Outputs (response_format).

        The model is guaranteed to return valid JSON matching the provided schema.

        Args:
            messages: Chat messages to send.
            json_schema: JSON Schema dict describing the expected output structure.
                Must include 'name' and 'schema' keys per OpenAI Structured Outputs spec.
            temperature: Override default temperature.
            max_tokens: Override default max tokens.

        Returns:
            LLMResponse with structured JSON content.

        Example:
            schema = {
                "name": "weather_response",
                "schema": {
                    "type": "object",
                    "properties": {
                        "temperature": {"type": "number"},
                        "condition": {"type": "string"},
                    },
                    "required": ["temperature", "condition"],
                },
            }
            response = await gateway.complete_structured(
                [LLMMessage(role="user", content="What's the weather?")],
                json_schema=schema,
            )
        """
        response_format: dict[str, object] = {
            "type": "json_schema",
            "json_schema": json_schema,
        }
        return await self.complete(
            messages, temperature, max_tokens, response_format
        )

    async def _call_provider(
        self,
        state: ProviderState,
        messages: Sequence[LLMMessage],
        temperature: float | None,
        max_tokens: int | None,
        response_format: dict[str, object] | None,
    ) -> LLMResponse:
        """Call a single provider with exponential backoff retry logic."""
        config = state.config
        temp = (
            temperature
            if temperature is not None
            else self._config.default_temperature
        )
        max_tok = (
            max_tokens if max_tokens is not None else self._config.default_max_tokens
        )

        # Ensure API key is set
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            raise LLMError(
                ErrorCode.LLM_CALL_FAILED,
                f"Missing API key for provider '{config.name}'. "
                f"Set {config.api_key_env}.",
            )

        from litellm import acompletion

        payload = [msg.model_dump() for msg in messages]
        kwargs: dict[str, object] = {
            "model": config.model,
            "messages": payload,
            "temperature": temp,
            "max_tokens": max_tok,
            "client": self._http_client,
        }

        if config.api_base:
            kwargs["api_base"] = config.api_base

        if response_format:
            kwargs["response_format"] = response_format

        last_error: Exception | None = None
        for attempt in range(config.max_retries + 1):
            start = time.monotonic()
            try:
                raw_response = await acompletion(**kwargs)  # type: ignore[arg-type]
                latency = time.monotonic() - start

                # Extract response content from litellm's unified format
                if isinstance(raw_response, dict):
                    response_dict = raw_response
                elif hasattr(raw_response, "model_dump"):
                    response_dict = raw_response.model_dump()
                else:
                    response_dict = dict(raw_response)  # type: ignore[call-overload]

                choices: list[dict[str, object]] = response_dict.get("choices", [])  # type: ignore[assignment]
                if not choices:
                    raise LLMError(
                        ErrorCode.LLM_CALL_FAILED, "LLM returned no choices"
                    )

                first_choice = choices[0]
                message = first_choice.get("message", {})
                content = str(message.get("content", ""))  # type: ignore[union-attr]
                usage = _parse_usage(response_dict.get("usage", {}))

                result = LLMResponse(
                    content=content, model=config.model, usage=usage
                )
                object.__setattr__(result, "_latency", latency)
                return result

            except LLMError:
                raise
            except Exception as exc:
                last_error = exc
                if attempt < config.max_retries:
                    wait = 2**attempt * 0.5  # 0.5, 1, 2, 4, ... seconds
                    logger.debug(
                        "Provider '%s' retry %d/%d after %.1fs",
                        config.name,
                        attempt + 1,
                        config.max_retries,
                        wait,
                    )
                    await asyncio.sleep(wait)

        raise LLMError(
            ErrorCode.LLM_CALL_FAILED,
            f"Provider '{config.name}' failed after {config.max_retries + 1} "
            f"attempts: {_sanitize_error_message(str(last_error))}",
        ) from last_error

    async def close(self) -> None:
        """Close the shared HTTP client and release all connections."""
        await self._http_client.aclose()

    def get_health(self) -> dict[str, dict[str, object]]:
        """Return health statistics for all providers."""
        return self._provider_manager.get_stats()