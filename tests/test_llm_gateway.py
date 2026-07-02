"""Tests for the LLM Gateway: CircuitBreaker, ProviderManager, and LLMGateway."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from athena.exceptions import ErrorCode, LLMError
from athena.infra.llm import LLMMessage, LLMResponse
from athena.infra.llm_gateway import (
    CircuitBreaker,
    CircuitState,
    LLMGateway,
    LLMGatewayConfig,
    LLMProviderConfig,
    ProviderManager,
    ProviderState,
)


# ============================================================
# CircuitBreaker Tests
# ============================================================


class TestCircuitBreaker:
    """Tests for the CircuitBreaker state machine."""

    def test_initial_state_is_closed(self) -> None:
        cb = CircuitBreaker()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_opens_after_threshold_failures(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_does_not_open_below_threshold(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_success_resets_to_closed(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(2):
            cb.record_failure()
        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.failure_count == 0

    def test_opens_after_success_then_more_failures(self) -> None:
        cb = CircuitBreaker(failure_threshold=3)
        cb.record_failure()
        cb.record_success()
        for _ in range(3):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN

    def test_half_open_after_recovery_timeout(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.01)
        for _ in range(2):
            cb.record_failure()
        assert cb.state == CircuitState.OPEN
        # Wait for recovery timeout
        time.sleep(0.02)
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.01)
        for _ in range(2):
            cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()  # Enter HALF_OPEN
        cb.record_success()
        assert cb.state == CircuitState.CLOSED

    def test_half_open_failure_opens_again(self) -> None:
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=0.01)
        for _ in range(2):
            cb.record_failure()
        time.sleep(0.02)
        cb.allow_request()  # Enter HALF_OPEN
        cb.record_failure()
        assert cb.state == CircuitState.OPEN


# ============================================================
# ProviderManager Tests
# ============================================================


class TestProviderManager:
    """Tests for the ProviderManager load balancing and failover."""

    def _make_providers(self, count: int = 3) -> list[LLMProviderConfig]:
        return [
            LLMProviderConfig(
                name=f"provider-{i}",
                provider="openai",
                model="gpt-4o",
                api_key_env="OPENAI_API_KEY",
                weight=float(i + 1),
            )
            for i in range(count)
        ]

    @pytest.mark.asyncio
    async def test_selects_healthy_provider(self) -> None:
        configs = self._make_providers(2)
        pm = ProviderManager(configs, strategy="weighted")
        state = await pm.select_provider()
        assert state.config.name in ("provider-0", "provider-1")

    @pytest.mark.asyncio
    async def test_skips_unhealthy_provider(self) -> None:
        configs = self._make_providers(2)
        pm = ProviderManager(configs, strategy="weighted")
        # Mark provider-0 as unhealthy
        pm._providers["provider-0"].circuit_breaker.state = CircuitState.OPEN
        state = await pm.select_provider()
        assert state.config.name == "provider-1"

    @pytest.mark.asyncio
    async def test_last_resort_when_all_unhealthy(self) -> None:
        configs = self._make_providers(2)
        pm = ProviderManager(configs, strategy="weighted")
        for p in pm._providers.values():
            p.circuit_breaker.state = CircuitState.OPEN
        state = await pm.select_provider()
        # Should still return a provider (last resort)
        assert state is not None

    @pytest.mark.asyncio
    async def test_round_robin_selection(self) -> None:
        configs = self._make_providers(3)
        pm = ProviderManager(configs, strategy="round_robin")
        selected = set()
        for _ in range(10):
            state = await pm.select_provider()
            selected.add(state.config.name)
        # With 3 providers, all should be selected at least once
        assert len(selected) == 3

    @pytest.mark.asyncio
    async def test_weighted_selection_favors_higher_weight(self) -> None:
        configs = [
            LLMProviderConfig(
                name="heavy",
                provider="openai",
                model="gpt-4o",
                api_key_env="OPENAI_API_KEY",
                weight=10.0,
            ),
            LLMProviderConfig(
                name="light",
                provider="openai",
                model="gpt-4o",
                api_key_env="OPENAI_API_KEY",
                weight=0.1,
            ),
        ]
        pm = ProviderManager(configs, strategy="weighted")
        heavy_count = 0
        iterations = 200
        for _ in range(iterations):
            state = await pm.select_provider()
            if state.config.name == "heavy":
                heavy_count += 1
        # Heavy should be selected much more often than light
        assert heavy_count > iterations * 0.7  # > 70% of selections

    @pytest.mark.asyncio
    async def test_record_result_updates_stats(self) -> None:
        configs = self._make_providers(1)
        pm = ProviderManager(configs)
        await pm.record_result("provider-0", success=True, latency=0.5)
        stats = pm.get_stats()
        assert stats["provider-0"]["total_requests"] == 1
        assert stats["provider-0"]["total_failures"] == 0
        assert stats["provider-0"]["avg_latency_ms"] == 500.0

    @pytest.mark.asyncio
    async def test_record_failure_updates_circuit_breaker(self) -> None:
        configs = self._make_providers(1)
        pm = ProviderManager(configs)
        pm._providers["provider-0"].circuit_breaker.failure_threshold = 2
        await pm.record_result("provider-0", success=False, latency=0.0)
        await pm.record_result("provider-0", success=False, latency=0.0)
        assert pm._providers["provider-0"].circuit_breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_raises_when_no_providers(self) -> None:
        with pytest.raises(LLMError, match="At least one LLM provider"):
            ProviderManager([])


# ============================================================
# LLMGateway Tests
# ============================================================


class TestLLMGateway:
    """Tests for the LLMGateway failover and structured outputs."""

    def _make_config(self) -> LLMGatewayConfig:
        return LLMGatewayConfig(
            providers=[
                LLMProviderConfig(
                    name="primary",
                    provider="openai",
                    model="gpt-4o",
                    api_key_env="OPENAI_API_KEY",
                    weight=3,
                ),
                LLMProviderConfig(
                    name="backup",
                    provider="openai",
                    model="gpt-4o-mini",
                    api_key_env="OPENAI_API_KEY",
                    weight=1,
                ),
            ],
            default_temperature=0.2,
            default_max_tokens=1024,
            load_balancing="weighted",
            circuit_breaker_failures=3,
            circuit_breaker_recovery=30.0,
        )

    @pytest.mark.asyncio
    async def test_complete_returns_llm_response(self) -> None:
        """Gateway should return LLMResponse when primary provider succeeds."""
        config = self._make_config()
        gateway = LLMGateway(config)

        mock_response = MagicMock()
        mock_response_dict = {
            "choices": [
                {"message": {"content": "Hello from primary"}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock_response.model_dump = lambda: mock_response_dict

        with patch("athena.infra.llm_gateway.acompletion", new_callable=AsyncMock) as mock_acomplete:
            mock_acomplete.return_value = mock_response
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                response = await gateway.complete(
                    [LLMMessage(role="user", content="Hello")]
                )

        assert isinstance(response, LLMResponse)
        assert response.content == "Hello from primary"
        assert response.model == "gpt-4o"
        await gateway.close()

    @pytest.mark.asyncio
    async def test_failover_to_backup_provider(self) -> None:
        """Gateway should fail over to backup when primary fails."""
        config = self._make_config()
        gateway = LLMGateway(config)

        mock_backup_response = MagicMock()
        mock_backup_response_dict = {
            "choices": [
                {"message": {"content": "Hello from backup"}}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
        }
        mock_backup_response.model_dump = lambda: mock_backup_response_dict

        call_count = 0

        async def mock_acomplete_side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("Primary provider failed")
            return mock_backup_response

        with patch("athena.infra.llm_gateway.acompletion", side_effect=mock_acomplete_side_effect):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                response = await gateway.complete(
                    [LLMMessage(role="user", content="Hello")]
                )

        assert response.content == "Hello from backup"
        assert response.model == "gpt-4o-mini"
        await gateway.close()

    @pytest.mark.asyncio
    async def test_raises_when_all_providers_fail(self) -> None:
        """Gateway should raise LLMError when all providers fail."""
        config = self._make_config()
        gateway = LLMGateway(config)

        async def mock_acomplete_always_fail(**kwargs):
            raise RuntimeError("Provider failed")

        with patch("athena.infra.llm_gateway.acompletion", side_effect=mock_acomplete_always_fail):
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                with pytest.raises(LLMError, match="All providers failed"):
                    await gateway.complete(
                        [LLMMessage(role="user", content="Hello")]
                    )

        await gateway.close()

    @pytest.mark.asyncio
    async def test_complete_structured_passes_response_format(self) -> None:
        """complete_structured should pass json_schema as response_format."""
        config = self._make_config()
        gateway = LLMGateway(config)

        mock_response = MagicMock()
        mock_response_dict = {
            "choices": [
                {"message": {"content": '{"temperature": 22, "condition": "sunny"}'}}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
        mock_response.model_dump = lambda: mock_response_dict

        schema = {
            "name": "weather",
            "schema": {
                "type": "object",
                "properties": {
                    "temperature": {"type": "number"},
                    "condition": {"type": "string"},
                },
                "required": ["temperature", "condition"],
            },
        }

        with patch("athena.infra.llm_gateway.acompletion", new_callable=AsyncMock) as mock_acomplete:
            mock_acomplete.return_value = mock_response
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                response = await gateway.complete_structured(
                    [LLMMessage(role="user", content="What's the weather?")],
                    json_schema=schema,
                )

        # Verify response_format was passed correctly
        call_kwargs = mock_acomplete.call_args[1]
        assert "response_format" in call_kwargs
        assert call_kwargs["response_format"]["type"] == "json_schema"
        assert call_kwargs["response_format"]["json_schema"] == schema

        assert response.content == '{"temperature": 22, "condition": "sunny"}'
        await gateway.close()

    @pytest.mark.asyncio
    async def test_raises_when_api_key_missing(self) -> None:
        """Gateway should raise LLMError when API key is not set."""
        config = self._make_config()
        gateway = LLMGateway(config)

        with patch.dict("os.environ", {}, clear=True):
            with pytest.raises(LLMError, match="Missing API key"):
                await gateway.complete(
                    [LLMMessage(role="user", content="Hello")]
                )

        await gateway.close()

    def test_get_health_returns_stats(self) -> None:
        """get_health should return provider statistics."""
        config = self._make_config()
        gateway = LLMGateway(config)
        health = gateway.get_health()
        assert "primary" in health
        assert "backup" in health
        assert "state" in health["primary"]
        assert health["primary"]["state"] == "closed"
        gateway.close_async = gateway.close
        asyncio.get_event_loop().run_until_complete(gateway.close())

    @pytest.mark.asyncio
    async def test_overrides_temperature_and_max_tokens(self) -> None:
        """Gateway should use overridden temperature and max_tokens."""
        config = self._make_config()
        gateway = LLMGateway(config)

        mock_response = MagicMock()
        mock_response_dict = {
            "choices": [
                {"message": {"content": "OK"}}
            ],
            "usage": {"total_tokens": 10},
        }
        mock_response.model_dump = lambda: mock_response_dict

        with patch("athena.infra.llm_gateway.acompletion", new_callable=AsyncMock) as mock_acomplete:
            mock_acomplete.return_value = mock_response
            with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
                await gateway.complete(
                    [LLMMessage(role="user", content="Hello")],
                    temperature=0.8,
                    max_tokens=512,
                )

        call_kwargs = mock_acomplete.call_args[1]
        assert call_kwargs["temperature"] == 0.8
        assert call_kwargs["max_tokens"] == 512
        await gateway.close()


# ============================================================
# LLMClient Protocol Compatibility
# ============================================================


@pytest.mark.asyncio
async def test_gateway_satisfies_llm_client_protocol() -> None:
    """LLMGateway should be usable wherever LLMClient is expected."""
    from athena.infra.llm import LLMClient

    config = LLMGatewayConfig(
        providers=[
            LLMProviderConfig(
                name="test",
                provider="openai",
                model="gpt-4o",
                api_key_env="OPENAI_API_KEY",
            ),
        ],
    )
    gateway = LLMGateway(config)

    # Verify it satisfies the Protocol (structural typing)
    assert hasattr(gateway, "complete")
    assert callable(gateway.complete)

    mock_response = MagicMock()
    mock_response_dict = {
        "choices": [{"message": {"content": "test"}}],
        "usage": {"total_tokens": 5},
    }
    mock_response.model_dump = lambda: mock_response_dict

    async def use_client(client: LLMClient) -> LLMResponse:
        return await client.complete([LLMMessage(role="user", content="test")])

    with patch("athena.infra.llm_gateway.acompletion", new_callable=AsyncMock) as mock_acomplete:
        mock_acomplete.return_value = mock_response
        with patch.dict("os.environ", {"OPENAI_API_KEY": "sk-test"}):
            response = await use_client(gateway)

    assert response.content == "test"
    await gateway.close()