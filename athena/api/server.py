"""Composition root for the Agent Runtime HTTP API.

Only the public Runtime, Ecommerce adapter, and governed Skill lifecycle are
mounted here.  Domain adapters are intentionally kept outside this module.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from collections.abc import AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from athena.api.errors import ApiServiceError
from athena.api.middleware import (
    install_metrics_middleware,
    install_rate_limit_middleware,
    install_trace_middleware,
)
from athena.api.repositories import Database, SkillRepository
from athena.api.repositories.skill_candidate_repository import SkillCandidateRepository
from athena.api.repositories.skill_evaluation_repository import (
    SkillEvaluationRepository,
)
from athena.api.repositories.skill_release_repository import SkillReleaseRepository
from athena.api.response import get_trace_id
from athena.api.routes import ecommerce, health, runtime_learning, runtime_tasks
from athena.api.routes import skill_candidates, skill_evaluation, skill_release
from athena.api.schemas import ErrorResponse
from athena.application.ecommerce_skill_trace import EcommerceSkillTraceService
from athena.application.runtime_learning_service import RuntimeLearningService
from athena.application.runtime_task_service import RuntimeTaskService
from athena.application.shadow_traffic_service import ShadowTrafficService
from athena.application.skill_candidate_generation_service import (
    SkillCandidateGenerationService,
)
from athena.application.skill_candidate_service import SkillCandidateService
from athena.application.skill_evaluation_service import SkillEvaluationService
from athena.application.skill_release_service import SkillReleaseService
from athena.config import AthenaSettings, load_settings
from athena.infra.cache import RedisCache, create_cache
from athena.infra.resilience import HierarchicalRateLimiter
from athena.observability.prometheus import PrometheusMetrics
from athena.runtime import RuntimeAssembly, build_runtime
from athena.api.repositories.shadow_traffic_repository import ShadowTrafficRepository

logger = logging.getLogger(__name__)


def _static_directory() -> Path:
    return Path(__file__).parent.parent / "web" / "static"


def _skill_candidate_tool_names() -> tuple[str, ...]:
    from athena.backend import ECOMMERCE_READONLY_TOOL_DEFINITIONS
    from athena.evaluation.backend_replay import fixed_ecommerce_diagnosis_cases

    names = [item.name for item in ECOMMERCE_READONLY_TOOL_DEFINITIONS]
    names.extend(
        call.tool_name
        for case in fixed_ecommerce_diagnosis_cases()
        for call in case.tool_call_plan
        if call.tool_name not in case.safety_oracle.forbidden_tool_names
    )
    return tuple(dict.fromkeys(names))


def _build_candidate_generator(settings: AthenaSettings) -> object | None:
    if not settings.llm.enabled:
        return None
    try:
        from athena.infra.llm import LLMClientFactory
        from athena.infra.model_router import ModelRouter
        from athena.infra.resilience import (
            ResilientLLMClient,
            RetryPolicy,
            make_breaker,
        )
        from athena.learning.candidate_generation import LLMCandidateGenerator

        def client(model: str, name: str) -> object:
            return ResilientLLMClient(
                LLMClientFactory.create(
                    provider=settings.llm.provider,
                    model=model,
                    temperature=0.0,
                    max_tokens=settings.llm.max_tokens,
                ),
                retry_policy=RetryPolicy(),
                breaker=make_breaker(name),
            )

        if settings.llm.routing.enabled:
            routed = ModelRouter(
                client(settings.llm.routing.light_model, "candidate-light"),
                client(settings.llm.routing.heavy_model, "candidate-heavy"),
                threshold=settings.llm.routing.threshold,
            )
            return LLMCandidateGenerator(routed)
        return LLMCandidateGenerator(client(settings.llm.model, "candidate"))
    except Exception as exc:  # provider configuration is optional in local mode
        logger.info("Skill candidate generator unavailable: %s", exc)
        return None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    database = getattr(app.state, "database", None)
    if database is not None and app.state.settings.database.auto_migrate:
        await database.create_schema()
    yield
    app.state.draining = True
    cache = getattr(app.state, "cache", None)
    if cache is not None:
        try:
            cache.close()
        except Exception:  # noqa: BLE001
            logger.warning("cache close failed", exc_info=True)
    if database is not None:
        await database.dispose()
    engine = getattr(app.state, "runtime_sync_engine", None)
    if engine is not None:
        engine.dispose()


def create_app(settings: AthenaSettings | None = None) -> FastAPI:
    resolved = settings or load_settings()
    app = FastAPI(title="Athena Agent Runtime", version="1.0.0", lifespan=_lifespan)
    app.state.settings = resolved
    app.state.draining = False
    app.state.cache = create_cache(
        resolved.cache.redis_url, namespace=resolved.cache.namespace
    )
    app.state.cache_component = {
        "component": "cache",
        "configured_backend": "redis" if resolved.cache.redis_url else "memory",
        "active_backend": (
            "redis" if isinstance(app.state.cache, RedisCache) else "memory"
        ),
        "status": "healthy",
        "reason_code": None,
    }
    app.state.database = None
    app.state.skill_candidate_repository = None
    app.state.skill_candidate_service = None
    app.state.skill_candidate_generation_service = None
    app.state.skill_evaluation_service = None
    app.state.skill_release_service = None
    app.state.shadow_traffic_service = None

    if resolved.database.url:
        database = Database(resolved.database)
        app.state.database = database
        candidate_repository = SkillCandidateRepository(database.session_factory)
        app.state.skill_candidate_repository = candidate_repository
        app.state.skill_candidate_service = SkillCandidateService(
            candidate_repository,
            allowed_readonly_tool_names=_skill_candidate_tool_names(),
        )
        app.state.ecommerce_skill_trace_service = EcommerceSkillTraceService(
            candidate_repository
        )
        evaluation_repository = SkillEvaluationRepository(database.session_factory)
        app.state.skill_evaluation_service = SkillEvaluationService(
            evaluation_repository, candidate_repository=candidate_repository
        )
        skill_repository = SkillRepository(database.session_factory)
        app.state.skill_release_service = SkillReleaseService(
            candidate_repository,
            evaluation_repository,
            skill_repository,
            SkillReleaseRepository(database.session_factory),
        )
        generator = _build_candidate_generator(resolved)
        if generator is not None:
            app.state.skill_candidate_generation_service = (
                SkillCandidateGenerationService(
                    candidate_repository,
                    app.state.skill_candidate_service,
                    generator,
                )
            )

    assembly: RuntimeAssembly = build_runtime(resolved)
    app.state.runtime_store = assembly.store
    app.state.agent_runtime = assembly.runtime
    app.state.runtime_backend = assembly.backend
    app.state.runtime_decision_mode = assembly.decision_mode
    app.state.runtime_sync_engine = assembly.sync_engine
    app.state.runtime_task_service = RuntimeTaskService(
        assembly.runtime,
        assembly.store,
        backend=assembly.backend,
        decision_mode=assembly.decision_mode,
        memory_strategy=assembly.memory_strategy,
        episodic_memory=assembly.episodic_memory,
        semantic_memory=assembly.semantic_memory,
    )
    app.state.runtime_learning_service = RuntimeLearningService(
        assembly.store, app.state.skill_candidate_repository
    )
    if (
        app.state.database is not None
        and app.state.skill_candidate_repository is not None
    ):
        traffic_repository = ShadowTrafficRepository(app.state.database.session_factory)
        app.state.shadow_traffic_service = ShadowTrafficService(
            traffic_repository,
            app.state.skill_candidate_repository,
            assembly.store,
        )

    app.state.prometheus = PrometheusMetrics()
    app.state.rate_limiter = HierarchicalRateLimiter(
        app.state.cache,
        global_per_minute=resolved.rate_limit.global_per_minute,
        per_tenant_per_minute=resolved.rate_limit.per_tenant_per_minute,
        per_route_per_minute=resolved.rate_limit.per_route_per_minute,
        burst_multiplier=resolved.rate_limit.burst_multiplier,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved.web.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    install_trace_middleware(app)
    if resolved.observability.metrics_enabled:
        install_metrics_middleware(app, app.state.prometheus)
    if resolved.rate_limit.enabled:
        install_rate_limit_middleware(app, app.state.rate_limiter)
    _configure_exception_handlers(app)
    _mount_routes(app)
    _mount_static(app, _static_directory())
    return app


def _configure_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiServiceError)
    async def handle_api_error(_: Request, exc: ApiServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=ErrorResponse(
                error_code=exc.error_code,
                message=exc.message,
                trace_id=get_trace_id(),
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("unexpected API error: %s", exc)
        return JSONResponse(
            status_code=500,
            content=ErrorResponse(
                error_code="INTERNAL_ERROR",
                message="Internal server error",
                trace_id=get_trace_id(),
            ).model_dump(),
        )


def _mount_routes(app: FastAPI) -> None:
    app.include_router(runtime_tasks.router)
    app.include_router(runtime_learning.router)
    app.include_router(ecommerce.router)
    app.include_router(skill_candidates.router)
    app.include_router(skill_evaluation.router)
    app.include_router(skill_release.router)
    app.include_router(health.router)


def _mount_static(app: FastAPI, directory: Path) -> None:
    if not directory.exists():
        return
    app.mount("/static", StaticFiles(directory=directory), name="static")

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(directory / "index.html")


__all__ = ["create_app"]
