"""Agent composition root shared by CLI, API and Worker code."""

from __future__ import annotations

from pathlib import Path

from athena.agent import ReActAgent
from athena.config import load_settings
from athena.infra.llm import LLMClientFactory
from athena.infra.model_router import ModelRouter
from athena.infra.resilience import (
    ResilientLLMClient,
    RetryPolicy,
    default_fault_diagnose_fallback,
    make_breaker,
)
from athena.logging import configure_logging
from athena.memory import WorkingMemory
from athena.prompt import ContextAssembler
from athena.tools import ToolRegistry
from athena.tools.builtin.basic import register_basic_tools
from athena.tools.cloud.k8s import register_k8s_readonly_tools


def build_agent(
    config_path: Path | None = None,
    llm_provider: str | None = None,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
) -> ReActAgent:
    """Build a fully wired ReAct agent from configuration and overrides."""
    settings = load_settings(config_path)
    configure_logging(settings.logging.level)

    registry = ToolRegistry()
    register_basic_tools(registry)
    register_k8s_readonly_tools(registry, settings=settings)

    resolved_provider = llm_provider or settings.llm.provider
    resolved_model = llm_model or settings.llm.model
    llm_client = LLMClientFactory.create(
        provider=resolved_provider,
        model=resolved_model,
        temperature=settings.llm.temperature,
        max_tokens=settings.llm.max_tokens,
        api_key=llm_api_key,
    )
    if settings.llm.routing.enabled and llm_model is None:
        light = LLMClientFactory.create(
            provider=resolved_provider,
            model=settings.llm.routing.light_model,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
            api_key=llm_api_key,
        )
        heavy = LLMClientFactory.create(
            provider=resolved_provider,
            model=settings.llm.routing.heavy_model,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
            api_key=llm_api_key,
        )
        llm_client = ModelRouter(light, heavy, threshold=settings.llm.routing.threshold)

    llm_client = ResilientLLMClient(
        llm_client,
        retry_policy=RetryPolicy(),
        breaker=make_breaker("llm"),
        fallback=default_fault_diagnose_fallback,
    )

    return ReActAgent(
        llm_client=llm_client,
        prompt_assembler=ContextAssembler(),
        tool_registry=registry,
        memory=WorkingMemory(max_tokens=settings.memory.working_max_tokens),
        max_steps=settings.agent.max_steps,
    )
