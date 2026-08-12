"""
OpenTelemetry 全链路追踪初始化。

将 FastAPI 请求自动埋点，并把我们自研的 trace_id（X-Trace-Id）作为 span 属性写入，
实现自研链路 ID 与 OTel span 的关联。无 OTLP collector 时降级为 Console 导出器，
保证本地演示可见 span，不阻塞启动。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_TRACER_PROVIDER = None
_SQLALCHEMY_INSTRUMENTED = False
_REDIS_INSTRUMENTED = False


def setup_tracing(
    app,
    service_name: str = "athena-agent",
    otlp_endpoint: str | None = None,
    database_engine=None,
):
    """
    初始化 OTel TracerProvider 并对 FastAPI 应用埋点。

    otlp_endpoint 为空时使用 Console 导出器（本地可见）；配置了 endpoint 则尝试 OTLP，
    导入失败时静默降级为 Console，避免因缺少 exporter 依赖而崩溃。
    幂等：重复调用只初始化一次 provider。
    """
    global _TRACER_PROVIDER, _REDIS_INSTRUMENTED, _SQLALCHEMY_INSTRUMENTED
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import (
            BatchSpanProcessor,
            ConsoleSpanExporter,
        )
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    except ImportError as exc:  # 依赖缺失时不影响主服务
        logger.warning("OpenTelemetry 未安装，跳过链路追踪: %s", exc)
        return None

    if _TRACER_PROVIDER is None:
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )
        exporter = None
        if otlp_endpoint:
            try:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
                    OTLPSpanExporter,
                )

                exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
            except ImportError:
                logger.warning("OTLP exporter 不可用，降级为 Console")
        if exporter is None:
            exporter = ConsoleSpanExporter()
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        _TRACER_PROVIDER = provider

    # 将自研 trace_id 注入到每个 server span，实现关联
    def _hook(span, scope):
        try:
            headers = dict(scope.get("headers") or [])
            raw = headers.get(b"x-trace-id")
            if raw:
                span.set_attribute("athena.trace_id", raw.decode("latin-1"))
            traceparent = headers.get(b"traceparent")
            if traceparent:
                span.set_attribute("athena.traceparent", traceparent.decode("latin-1"))
        except Exception:  # 埋点失败不能影响请求
            pass

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=_TRACER_PROVIDER, server_request_hook=_hook
    )
    if database_engine is not None and not _SQLALCHEMY_INSTRUMENTED:
        try:
            from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

            SQLAlchemyInstrumentor().instrument(engine=database_engine.sync_engine)
            _SQLALCHEMY_INSTRUMENTED = True
        except Exception as exc:  # noqa: BLE001 - observability must not block startup
            logger.warning("SQLAlchemy tracing setup failed: %s", exc)
    if not _REDIS_INSTRUMENTED:
        try:
            from opentelemetry.instrumentation.redis import RedisInstrumentor

            RedisInstrumentor().instrument()
            _REDIS_INSTRUMENTED = True
        except Exception as exc:  # noqa: BLE001 - redis may be unavailable in demo mode
            logger.warning("Redis tracing setup failed: %s", exc)
    logger.info("OpenTelemetry 链路追踪已启用 service=%s", service_name)
    return _TRACER_PROVIDER
