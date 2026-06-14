from __future__ import annotations
import structlog

logger = structlog.get_logger(__name__)


def init(app, service: str, settings) -> None:
    if not settings.otel_endpoint:
        logger.info("tracing.disabled", reason="no OTLP endpoint")
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        provider = TracerProvider(resource=Resource.create(
            {"service.name": service, "service.version": settings.service_version,
             "deployment.environment": settings.env}))
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=settings.otel_endpoint, insecure=True)))
        trace.set_tracer_provider(provider)
        FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,readyz,metrics")
        HTTPXClientInstrumentor().instrument()
        logger.info("tracing.enabled", endpoint=settings.otel_endpoint)
    except Exception as e:
        logger.warning("tracing.init_failed", error=str(e))
