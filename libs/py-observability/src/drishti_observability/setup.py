from __future__ import annotations
from fastapi import FastAPI

from drishti_observability import sentry, tracing
from drishti_observability.config import ObsSettings
from drishti_observability.logging import configure
from drishti_observability.metrics import metrics_endpoint
from drishti_observability.middleware import ObservabilityMiddleware


def setup(app: FastAPI, service: str, settings: ObsSettings | None = None) -> None:
    s = settings or ObsSettings.load()
    configure(service, level=s.log_level, json_logs=s.log_json, version=s.service_version)
    sentry.init(service, s)
    app.add_middleware(ObservabilityMiddleware, service=service)
    tracing.init(app, service, s)
    app.add_route("/metrics", lambda req: metrics_endpoint(), include_in_schema=False)
