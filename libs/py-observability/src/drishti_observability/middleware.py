from __future__ import annotations
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from drishti_observability.logging import request_id_var, user_id_var
from drishti_observability.metrics import HTTP_LATENCY, HTTP_REQUESTS

logger = structlog.get_logger("http")
_SKIP = {"/healthz", "/readyz", "/metrics"}


class ObservabilityMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, service: str) -> None:
        super().__init__(app)
        self.service = service

    async def dispatch(self, request: Request, call_next) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        rtok = request_id_var.set(rid)
        utok = user_id_var.set(request.headers.get("x-user-id", ""))
        structlog.contextvars.bind_contextvars(request_id=rid)

        path = request.url.path
        skip = path in _SKIP
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            response.headers["x-request-id"] = rid
            return response
        finally:
            dur = time.perf_counter() - start
            if not skip:
                route = request.scope.get("route")
                label = getattr(route, "path", path) if route else path
                HTTP_REQUESTS.labels(self.service, request.method, label, str(status)).inc()
                HTTP_LATENCY.labels(self.service, request.method, label).observe(dur)
                logger.info("http.access", method=request.method, path=path,
                            status=status, duration_ms=round(dur * 1000, 1))
            request_id_var.reset(rtok)
            user_id_var.reset(utok)
            structlog.contextvars.clear_contextvars()
