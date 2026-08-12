"""Runtime dependency assembly for Demo, local durable, and provider-backed modes."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from athena.api.repositories.models import Base
from athena.config import AthenaSettings

from .durable import DurableRuntimeStore
from .engine import DecisionEngine, DemoDecisionEngine
from .llm_engine import LLMDecisionEngine
from .memory import FourLayerRuntimeContextCompiler
from .runtime import AgentRuntime
from .store import InMemoryRuntimeStore
from .tools import ReadOnlyToolCatalog

logger = logging.getLogger(__name__)


class HumanEscalationDecisionEngine:
    """Production fallback that cannot invent a tool call without a model."""

    def decide(self, context):
        from .models import Decision, DecisionKind

        return Decision(
            kind=DecisionKind.ASK_HUMAN,
            reason_code="LLM_DECISION_UNAVAILABLE",
            response="当前没有可用的受管模型，请配置模型后重新运行任务。",
        )


@dataclass(frozen=True)
class RuntimeAssembly:
    runtime: AgentRuntime
    store: Any
    backend: str
    decision_mode: str
    memory_strategy: str
    sync_engine: Any | None = None


def build_runtime(settings: AthenaSettings) -> RuntimeAssembly:
    tools = ReadOnlyToolCatalog()
    store: Any = InMemoryRuntimeStore()
    backend = "memory-demo"
    sync_engine = None

    if settings.database.url and settings.database.auto_migrate:
        try:
            sync_url = _sync_url(settings.database.url)
            options: dict[str, object] = {
                "echo": settings.database.echo,
                "pool_pre_ping": True,
            }
            if sync_url.startswith("sqlite:///"):
                options["connect_args"] = {"check_same_thread": False}
            if sync_url.startswith("sqlite:///:memory:"):
                options["poolclass"] = StaticPool
            sync_engine = create_engine(sync_url, **options)
            Base.metadata.create_all(sync_engine)
            store = DurableRuntimeStore(
                sessionmaker(sync_engine, expire_on_commit=False),
                lease_seconds=settings.worker.lease_ttl_seconds,
            )
            backend = "sqlite-durable" if sync_url.startswith("sqlite") else "sql-durable"
        except Exception as exc:  # noqa: BLE001 - local startup must remain usable
            logger.warning("Runtime durable store unavailable; using memory adapter: %s", exc)
            if sync_engine is not None:
                sync_engine.dispose()
                sync_engine = None

    engine, decision_mode = _build_decision_engine(settings)
    runtime = AgentRuntime(
        store=store,
        decision_engine=engine,
        context_compiler=FourLayerRuntimeContextCompiler(
            model_window_tokens=16_384,
            safety_margin_tokens=1_024,
        ),
        tools=tools,
    )
    return RuntimeAssembly(
        runtime=runtime,
        store=store,
        backend=backend,
        decision_mode=decision_mode,
        memory_strategy="working-summary-evidence-evaluated-skill",
        sync_engine=sync_engine,
    )


def _build_decision_engine(settings: AthenaSettings) -> tuple[DecisionEngine, str]:
    if not settings.llm.enabled:
        logger.info("Runtime LLM disabled; using deterministic demo decision engine")
        if settings.runtime.profile == "production":
            return HumanEscalationDecisionEngine(), "human-escalation"
        return DemoDecisionEngine(), "deterministic-demo"
    try:
        from athena.infra.llm import LLMClientFactory
        from athena.infra.model_router import ModelRouter

        light_model = (
            settings.llm.routing.light_model
            if settings.llm.routing.enabled
            else settings.llm.model
        )
        heavy_model = (
            settings.llm.routing.heavy_model
            if settings.llm.routing.enabled
            else settings.llm.model
        )
        light = LLMClientFactory.create(
            provider=settings.llm.provider,
            model=light_model,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
        )
        heavy = light if heavy_model == light_model else LLMClientFactory.create(
            provider=settings.llm.provider,
            model=heavy_model,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
        )
        return LLMDecisionEngine(
            ModelRouter(
                light,
                heavy,
                threshold=settings.llm.routing.threshold,
            )
        ), "llm-json"
    except Exception as exc:  # noqa: BLE001 - provider is optional in Demo
        logger.info("Runtime LLM unavailable, using safe fallback: %s", exc)
        if settings.runtime.profile == "production":
            return HumanEscalationDecisionEngine(), "human-escalation"
        return DemoDecisionEngine(), "deterministic-demo"


def _sync_url(url: str) -> str:
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url.removeprefix("sqlite+aiosqlite://")
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql+asyncpg://")
    if url.startswith("postgresql://"):
        return "postgresql+psycopg://" + url.removeprefix("postgresql://")
    return url
